import json,subprocess,zipfile,os,sys
from pathlib import Path
def run(args):subprocess.run(args,check=True)
root=Path('.state/mcp-lambda-layers');root.mkdir(exist_ok=True)
core=root/'core';pricing=root/'pricing'
run([sys.executable,'-m','pip','install','--target',str(core/'python'),'-r','tools/mcp_lambda/requirements-core.txt'])
run([sys.executable,'-m','pip','install','--target',str(pricing/'pricing-mcp'),'-r','tools/pricing/requirements.txt'])
run(['docker','pull','--platform','linux/amd64','hashicorp/terraform-mcp-server:1.3.0'])
cid=subprocess.check_output(['docker','create','--platform','linux/amd64','hashicorp/terraform-mcp-server:1.3.0'],text=True).strip()
(core/'bin').mkdir(exist_ok=True)
try:run(['docker','cp',cid+':/bin/terraform-mcp-server',str(core/'bin'/'terraform-mcp-server')])
finally:run(['docker','rm',cid])
(core/'bin'/'terraform-mcp-server').chmod(0o755)
for name,folder in [('core',core),('pricing',pricing)]:
    path=root/(name+'.zip')
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
        for f in folder.rglob('*'):
            if f.is_file() and '__pycache__' not in f.parts and f.suffix!='.pyc':archive.write(f,f.relative_to(folder))
    print(name,'zip MB',round(path.stat().st_size/1048576,2),'uncompressed MB',round(sum(i.file_size for i in zipfile.ZipFile(path).infolist())/1048576,2),flush=True)
