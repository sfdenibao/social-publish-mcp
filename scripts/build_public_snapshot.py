"""Build a source-only release without private runtime data or Git history."""
import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['README.md','README.en.md','LICENSE','THIRD_PARTY_NOTICES.md',
              '.gitignore','pyproject.toml','conf.example.py','sau_cli.py',
              'publish_mcp.py','mcp_acceptance_client.py','start_publish_mcp.pyw',
              'start_publish_mcp.ps1','install_autostart.ps1']
DOCS = ['MCP_INTEGRATION_GUIDE.md','MCP_TOOL_SCHEMAS.json','PARALLEL_PUBLISHING.md',
        'DOUYIN_AUTOREPLY.md','DOUYIN_COMMENTS.md']


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('destination')
    args=parser.parse_args()
    target=Path(args.destination).resolve()
    if target==ROOT or ROOT in target.parents:
        raise SystemExit('Destination must be outside the development tree')
    if target.exists() and any(target.iterdir()):
        raise SystemExit('Destination must be empty; no files are overwritten')
    files=[ROOT/p for p in ROOT_FILES]+[ROOT/'docs'/p for p in DOCS]
    for folder in ['uploader','utils','myUtils','tests','scripts']:
        files.extend(p for p in (ROOT/folder).rglob('*.py') if '__pycache__' not in p.parts)
    files.append(ROOT/'utils/stealth.min.js')
    files.extend((ROOT/'config').glob('*.example.json'))
    files.append(ROOT/'config/comment_reply_prompt.txt')
    files.extend(p for p in (ROOT/'LICENSES').iterdir() if p.is_file())
    workflow=ROOT/'.github/workflows/tests.yml'
    if workflow.exists():files.append(workflow)
    for src in sorted(set(files)):
        dest=target/src.relative_to(ROOT)
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(src,dest)
    print(f'Copied {len(set(files))} reviewed source/document files to {target}')


if __name__=='__main__':main()
