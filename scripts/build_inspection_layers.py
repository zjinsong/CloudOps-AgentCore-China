"""Build isolated official AWS API / AgentCore MCP Lambda dependencies on Linux."""
import subprocess
import sys
import zipfile
from pathlib import Path

def main():
    root=Path('.state/mcp-lambda-layers')
    for name, folder in [('aws-api','aws_api'),('agentcore-mcp','agentcore')]:
        destination=root/name/name
        destination.mkdir(parents=True,exist_ok=True)
        subprocess.run([sys.executable,'-m','pip','install','--upgrade','--target',str(destination),'-r',f'tools/{folder}/requirements.txt'],check=True)
        # These MCP groups do not use browser tooling; remove bundled browser
        # driver binaries and test/cache files to fit the Lambda layer quota.
        import shutil
        driver=destination/'playwright'/'driver'
        if driver.exists():shutil.rmtree(driver)
        if name=='aws-api':
            # Keep CLI plugin dependencies, compress service models using the
            # format natively supported by botocore's JSONFileLoader.
            import gzip
            data=destination/'awscli'/'botocore'/'data'
            for file in data.rglob('*.json'):
                if file.parent==data:continue
                with gzip.open(str(file)+'.gz','wb') as out:out.write(file.read_bytes())
                file.unlink()
            examples=destination/'awscli'/'examples'
            if examples.exists():shutil.rmtree(examples)
        with zipfile.ZipFile(root/(name+'.zip'),'w',zipfile.ZIP_DEFLATED) as archive:
            for file in (root/name).rglob('*'):
                if file.is_file() and '__pycache__' not in file.parts and file.suffix!='.pyc':
                    archive.write(file,file.relative_to(root/name))
        size=sum(i.file_size for i in zipfile.ZipFile(root/(name+'.zip')).infolist())
        print(name,'uncompressed MB',round(size/1048576,1),flush=True)

if __name__=='__main__':main()
