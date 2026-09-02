import os
import tarfile
from pathlib import Path

# The directory we want to package
SOURCE_DIR = "store"
# The output archive name
OUTPUT_FILE = "orgbrain_store.tar.gz"

# Folders and file extensions to ignore
EXCLUDE_DIRS = {'.git', '.pytest_cache', 'bronze'}
EXCLUDE_EXTS = {'.pyc', '.pyo', '.pyd', '.DS_Store'}

def should_exclude(file_path):
    path_obj = Path(file_path)
    
    # Check if any parent directory is in the exclude list
    for part in path_obj.parts:
        if part in EXCLUDE_DIRS:
            return True
            
    # Check file extensions
    if path_obj.suffix in EXCLUDE_EXTS or path_obj.name in EXCLUDE_EXTS:
        return True
        
    return False

def make_tarfile():
    print(f"📦 Packaging '{SOURCE_DIR}/' into '{OUTPUT_FILE}'...")
    
    with tarfile.open(OUTPUT_FILE, "w:gz") as tar:
        for root, dirs, files in os.walk(SOURCE_DIR):
            # Modify dirs in-place to prevent os.walk from even entering excluded directories
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            
            for file in files:
                file_path = os.path.join(root, file)
                if not should_exclude(file_path):
                    # Add the file to the tar archive
                    tar.add(file_path)
                    print(f"  Added: {file_path}")
                else:
                    print(f"  Ignored: {file_path}")
                    
    print(f"\n✅ Successfully created {OUTPUT_FILE}")
    print(f"\n🚀 Next steps:")
    print(f"1. Copy this single file to your VM using scp:")
    print(f"   scp {OUTPUT_FILE} username@your-vm-ip-address:/home/username/")
    print(f"2. SSH into your VM and extract it:")
    print(f"   tar -xzf {OUTPUT_FILE}")

if __name__ == "__main__":
    if not os.path.exists(SOURCE_DIR):
        print(f"❌ Error: Could not find directory '{SOURCE_DIR}'. Run this script from the project root.")
    else:
        make_tarfile()
