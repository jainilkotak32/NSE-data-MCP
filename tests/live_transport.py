"""Optional live-test transport for environments where curl alone has network access.

This does not ship in the server or bypass document validation. HTTPX still owns
cookies, redirects, status handling and body decoding; curl performs each request.
"""
import asyncio
from pathlib import Path
import tempfile

import httpx2


class CurlTransport(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request):
        with tempfile.TemporaryDirectory(prefix='nse-live-') as folder:
            headers_path = Path(folder) / 'headers'
            body_path = Path(folder) / 'body'
            command = ['curl', '--http1.1', '--max-time', '30', '-sS',
                       '-D', str(headers_path), '-o', str(body_path),
                       '-X', request.method, str(request.url)]
            for key, value in request.headers.multi_items():
                command.extend(['-H', f'{key}: {value}'])
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.PIPE)
            _, error = await process.communicate()
            if process.returncode:
                raise httpx2.ConnectError(error.decode(errors='replace'), request=request)
            blocks = headers_path.read_bytes().replace(b'\r\n', b'\n').strip().split(b'\n\n')
            block = next(block for block in reversed(blocks) if block.startswith(b'HTTP/'))
            lines = block.splitlines()
            status = int(lines[0].split()[1])
            headers = [tuple(part.strip() for part in line.split(b':', 1))
                       for line in lines[1:] if b':' in line]
            return httpx2.Response(status, headers=headers, content=body_path.read_bytes(), request=request)
