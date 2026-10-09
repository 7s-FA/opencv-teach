"""Install public example configuration without touching existing working data."""
from pathlib import Path
import shutil
ROOT=Path(__file__).resolve().parents[1]
target=ROOT/'data'
if target.exists():
    raise SystemExit('data/ already exists; existing configuration was left unchanged.')
shutil.copytree(ROOT/'examples/data',target)
for file in target.rglob('*.json'):
    text=file.read_text().replace('/path/to/Final_Arm/SO101_teach',str(ROOT))
    file.write_text(text)
print('Offline example configuration created. Start with: bash run.sh --demo')
