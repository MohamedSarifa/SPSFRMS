import shutil
import subprocess
import unittest
from pathlib import Path


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

async function render(script, payload, httpOk) {
    const labels = [];
    const indicators = [0, 1].map(() => {
        const label = { textContent: 'status' };
        labels.push(label);
        return {
            dataset: { healthUrl: '/health' },
            querySelector: selector => selector === '.status-text' ? label : null,
        };
    });
    let domReady;
    const document = {
        querySelectorAll: selector => selector === '[data-health-status]' ? indicators : [],
        addEventListener: (name, callback) => { if (name === 'DOMContentLoaded') domReady = callback; },
        querySelector: () => null,
        getElementById: () => null,
    };
    const fetch = async () => ({
        ok: httpOk,
        json: async () => payload,
    });
    vm.runInNewContext(script, { document, fetch, console });
    domReady();
    await new Promise(resolve => setImmediate(resolve));
    return labels.map(label => label.textContent);
}

(async () => {
    const script = fs.readFileSync(process.argv[1], 'utf8');
    const cases = [
        [{ database: 'connected', initialization: 'not_run', status: 'degraded' }, false,
            'Database connected'],
        [{ database: 'not connected', initialization: 'not_run', status: 'error' }, false,
            'Database unavailable'],
        [{ database: 'connected', initialization: 'failed', status: 'degraded' }, false,
            'Database initialization failed'],
        [{ database: 'connected', initialization: 'initialized', status: 'ok' }, true,
            'Database connected · Startup initialization completed'],
    ];
    for (const [payload, httpOk, expected] of cases) {
        const actual = await render(script, payload, httpOk);
        if (actual.some(label => label !== expected)) {
            throw new Error(`Expected both status indicators to show: ${expected}`);
        }
    }
})().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
});
"""


class FrontendHealthTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required to run the frontend status test")
    def test_health_status_messages_cover_database_and_initialization_states(self):
        project_root = Path(__file__).resolve().parents[1]
        script_path = project_root / "static" / "js" / "script.js"
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_HARNESS, str(script_path)],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
