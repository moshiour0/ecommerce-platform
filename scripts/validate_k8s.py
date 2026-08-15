#!/usr/bin/env python3
"""
Cross-check the Kubernetes manifests against the compose topology.

    python scripts/validate_k8s.py

No cluster is required. This does not replace `kubectl apply --dry-run=server`
-- it cannot catch admission-controller or CRD problems -- but it does catch
the failure mode that actually matters here: manifests that quietly stop
matching the configuration that is known to work. Every drift bug in this
project so far (connectors pointing at databases that do not exist, a rate
limiter whose fix was a comment, a schema that only existed in one volume) was
of exactly that shape.

Checks:
  1. every buildable compose service has a manifest, and vice versa
  2. container port == compose port == Service port/targetPort
  3. Service selector actually matches the Deployment's pod labels
  4. no plaintext credential anywhere in the tree
  5. every $(VAR) reference resolves to an env var defined EARLIER in the same
     container -- Kubernetes expands in list order, so a later definition
     silently renders the literal string
  6. probes, resource requests/limits and securityContext are present
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
K8S = REPO / "infrastructure" / "k8s"
INFRA = {"postgres", "redis", "kafka", "zookeeper", "elasticsearch",
         "schema-registry", "jaeger", "debezium"}
VAR_REF = re.compile(r"\$\((\w+)\)")
SECRET_SMELL = re.compile(r"supersecret|password\s*[:=]\s*['\"]?[a-z0-9]{6,}", re.I)

problems = []


def fail(msg):
    problems.append(msg)


def load_all():
    """dev-infra/ is deliberately excluded: it exists only so a local cluster
    has something to point at, and by design has no compose counterpart in the
    generated set."""
    docs = []
    for p in sorted(K8S.rglob("*.yaml")):
        if "dev-infra" in p.parts:
            continue
        for d in yaml.safe_load_all(p.read_text(encoding="utf-8")):
            if d:
                docs.append((p, d))
    return docs


def compose_services():
    res = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.apps.yml", "config"],
        cwd=REPO, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit("FATAL: docker compose config failed")
    cfg = yaml.safe_load(res.stdout)
    out = {}
    for n, s in cfg["services"].items():
        if n in INFRA or "build" not in s:
            continue
        ports = s.get("ports") or []
        out[n] = int(ports[0]["target"]) if ports else None
    return out


def main():
    docs = load_all()
    compose = compose_services()

    deploys = {d["metadata"]["name"]: (p, d) for p, d in docs if d["kind"] == "Deployment"}
    svcs = {d["metadata"]["name"]: (p, d) for p, d in docs if d["kind"] == "Service"}

    # 1. coverage both ways
    for name in compose:
        if name not in deploys:
            fail(f"compose service '{name}' has no Deployment manifest")
    for name in deploys:
        if name not in compose:
            fail(f"Deployment '{name}' has no matching compose service")

    for name, port in compose.items():
        if name not in deploys:
            continue
        path, dep = deploys[name]
        spec = dep["spec"]["template"]["spec"]
        c = spec["containers"][0]

        # 2. ports agree end to end
        cports = [p["containerPort"] for p in c.get("ports", [])]
        if port is None:
            if cports:
                fail(f"{name}: compose exposes no port but manifest declares {cports}")
        else:
            if cports != [port]:
                fail(f"{name}: containerPort {cports} != compose port {port}")
            if name not in svcs:
                fail(f"{name}: exposes port {port} but has no Service")
            else:
                sp = svcs[name][1]["spec"]["ports"][0]
                if sp["port"] != port or sp["targetPort"] != port:
                    fail(f"{name}: Service port {sp} != {port}")
                # 3. selector must match the pod labels, not just the app name
                sel = svcs[name][1]["spec"]["selector"]
                labels = dep["spec"]["template"]["metadata"]["labels"]
                if not all(labels.get(k) == v for k, v in sel.items()):
                    fail(f"{name}: Service selector {sel} does not match pod labels {labels}")

        # 5. $(VAR) ordering
        env = c.get("env", [])
        defined = []
        for e in env:
            for ref in VAR_REF.findall(str(e.get("value", ""))):
                if ref not in defined:
                    fail(f"{name}: env '{e['name']}' references $({ref}) "
                         f"before it is defined — Kubernetes expands in list order, "
                         f"so this renders literally")
            defined.append(e["name"])

        # 6. production hygiene
        if port is not None:
            for probe in ("readinessProbe", "livenessProbe"):
                if probe not in c:
                    fail(f"{name}: missing {probe}")
        r = c.get("resources", {})
        if not r.get("requests") or not r.get("limits"):
            fail(f"{name}: missing resource requests/limits")
        if not c.get("securityContext", {}).get("runAsNonRoot"):
            fail(f"{name}: container securityContext does not set runAsNonRoot")

    # 4. no plaintext credentials, anywhere
    for p in sorted(K8S.rglob("*.yaml")):
        if "dev-infra" in p.parts:
            continue
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if "REPLACE_ME" in line:
                continue
            if SECRET_SMELL.search(line):
                fail(f"{p.relative_to(REPO)}:{i}: possible plaintext credential")

    print(f"manifests: {len(docs)} documents, {len(deploys)} Deployments, {len(svcs)} Services")
    print(f"compose services covered: {len(compose)}")
    if problems:
        print(f"\nFAILED — {len(problems)} problem(s):")
        for x in problems:
            print(f"  - {x}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
