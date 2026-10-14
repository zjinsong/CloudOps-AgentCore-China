"""Install a pinned Terraform CLI, verifying the upstream SHA256 manifest."""
import hashlib
import platform
import urllib.request
import zipfile
import io
from pathlib import Path

version = '1.12.2'
arch = {'aarch64':'arm64', 'arm64':'arm64', 'x86_64':'amd64', 'AMD64':'amd64'}[platform.machine()]
name = f'terraform_{version}_linux_{arch}.zip'
base = f'https://releases.hashicorp.com/terraform/{version}/'
raw = urllib.request.urlopen(base + name, timeout=120).read()
checks = urllib.request.urlopen(base + f'terraform_{version}_SHA256SUMS', timeout=120).read().decode()
expected = next(line.split()[0] for line in checks.splitlines() if line.split()[-1].lstrip('*') == name)
if hashlib.sha256(raw).hexdigest() != expected:
    raise RuntimeError('Terraform SHA256 verification failed')
target = Path('/usr/local/bin/terraform')
target.write_bytes(zipfile.ZipFile(io.BytesIO(raw)).read('terraform'))
target.chmod(0o755)
