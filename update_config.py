import re

with open('backend/config.py', 'r') as f:
    content = f.read()

content = content.replace('GB_MOCK_MODE: bool = True', 'GB_MOCK_MODE: bool = False')

with open('backend/config.py', 'w') as f:
    f.write(content)

print("✓ GB_MOCK_MODE set to False")
