"""Export source-defined tool contracts with isolated storage and no workers."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix='publish-schema-') as temporary:
        os.environ['SAU_RUNTIME_DIR'] = temporary
        import publish_mcp
        tools = asyncio.run(publish_mcp.mcp.list_tools())
        output = dict(server='http://127.0.0.1:5410/mcp', transport='streamable-http',
                      generated_at=datetime.now(timezone.utc).isoformat(),
                      source='source registry; isolated runtime; no worker started',
                      tools=[tool.model_dump(mode='json') for tool in tools])
        target = ROOT / 'docs/MCP_TOOL_SCHEMAS.json'
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(f'Exported {len(tools)} tools to {target}')


if __name__ == '__main__':
    main()
