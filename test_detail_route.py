import sys
import threading
import types
import unittest
from unittest.mock import patch


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self):
        return lambda function: function

    def streamable_http_app(self):
        async def app(scope, receive, send):
            return None
        return app


def _load_server_without_background_integrations():
    original_start = threading.Thread.start

    def start_unless_playwright_worker(thread, *args, **kwargs):
        if thread.name == 'playwright-worker':
            return None
        return original_start(thread, *args, **kwargs)

    fake_mcp = types.ModuleType('mcp')
    fake_mcp.__path__ = []
    fake_mcp_server = types.ModuleType('mcp.server')
    fake_mcp_server.__path__ = []
    fake_fastmcp_module = types.ModuleType('mcp.server.fastmcp')
    fake_fastmcp_module.FastMCP = _FakeFastMCP
    stubs = {
        'mcp': fake_mcp,
        'mcp.server': fake_mcp_server,
        'mcp.server.fastmcp': fake_fastmcp_module,
    }

    with patch.dict(sys.modules, stubs), patch.object(
        threading.Thread, 'start', new=start_unless_playwright_worker
    ):
        import server

    return server


server = _load_server_without_background_integrations()


class DetailRouteTests(unittest.TestCase):
    def setUp(self):
        server.app.config['TESTING'] = True
        self.client = server.app.test_client()

    @patch.object(server, '_submit_job', return_value='detail-job')
    def test_preserves_selected_page_for_detail_worker(self, submit_job):
        response = self.client.post('/api/detalle', json={'index': 1, 'page': 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'job_id': 'detail-job'})
        submit_job.assert_called_once_with({
            'type': 'detalle',
            'index': 1,
            'page': 2,
        })

    @patch.object(server, '_submit_job', return_value='detail-job')
    def test_defaults_to_first_page_for_older_clients(self, submit_job):
        response = self.client.post('/api/detalle', json={'index': 1})

        self.assertEqual(response.status_code, 200)
        submit_job.assert_called_once_with({
            'type': 'detalle',
            'index': 1,
            'page': 1,
        })


if __name__ == '__main__':
    unittest.main()
