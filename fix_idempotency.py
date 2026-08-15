import os
import glob
import re

target_dir = r"c:\projects\ecommerce-platform\services"

replacement_basic = """    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(IdempotencyKey).values(key=idempotency_key).on_conflict_do_nothing()
    result = await db.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Idempotency key already processed")"""

pattern = re.compile(
    r"\s*existing_key = await db\.execute\(select\(IdempotencyKey\)\.where\(IdempotencyKey\.key == idempotency_key\)\)\s+"
    r"if existing_key\.scalar_one_or_none\(\):\s+"
    r"raise HTTPException\(status_code=409, detail=[^\n]+\)\s+"
    r"(?:#.*?[\r\n]+)*"
    r"(?:idem_key = IdempotencyKey\(key=idempotency_key\)[\r\n]+)?\s*(?:db\.add\(idem_key\)[\r\n]+)?",
    re.DOTALL
)

def fix_idempotency():
    for filepath in glob.glob(os.path.join(target_dir, "*", "app", "services", "*.py")):
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # We need to make sure we don't accidentally remove 'return idem_key' in cart_service.py if we don't replace it correctly, 
        # but cart_service.py has a slightly different pattern for the helper function check_idempotency.
        
        new_content = pattern.sub(f"\n{replacement_basic}\n", content)
        if new_content != content:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Fixed: {filepath}")
        else:
            print(f"Skipped: {filepath} (No match found)")

if __name__ == "__main__":
    fix_idempotency()
