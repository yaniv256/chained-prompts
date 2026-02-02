"""Config loader for chained-prompts MCP."""
import os
import json
import socket
from pathlib import Path

def detect_instance():
    hostname = socket.gethostname().lower()
    if any(x in hostname for x in ['jetson', 'orin', 'cloister']):
        return 'cloister'
    return 'wsl'

def get_config(instance=None):
    if instance is None:
        instance = os.environ.get('ZARA_INSTANCE', detect_instance())
    
    config_dir = Path(__file__).parent / 'config'
    config_file = config_dir / f'{instance}.json'
    
    if not config_file.exists():
        raise FileNotFoundError(f"Config not found: {config_file}")
    
    with open(config_file) as f:
        return json.load(f)

if __name__ == '__main__':
    config = get_config()
    print(f"Instance: {config['instance']}")
    for k, v in config['paths'].items():
        print(f"  {k}: {v}")
