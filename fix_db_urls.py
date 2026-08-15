import os
import glob
import re

target_dir = r"c:\projects\ecommerce-platform\services"

pattern = re.compile(r'DATABASE_URL\s*=\s*"([^"]+)"')

def fix_database_urls():
    for filepath in glob.glob(os.path.join(target_dir, "*", "app", "database.py")):
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        match = pattern.search(content)
        if match:
            old_url = match.group(1)
            replacement = f'import os\nDATABASE_URL = os.getenv("DATABASE_URL", "{old_url}")'
            new_content = pattern.sub(replacement, content)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Fixed URL in: {filepath}")
        else:
            print(f"No match found in: {filepath}")

if __name__ == "__main__":
    fix_database_urls()
