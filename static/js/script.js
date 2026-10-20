(function () {
    'use strict';

    function initHealthStatus() {
        const indicators = document.querySelectorAll('[data-health-status]');
        if (!indicators.length) return;
        const url = indicators[0].dataset.healthUrl;

        const setState = (state, text) => {
            indicators.forEach(el => {
                el.dataset.state = state;
                const label = el.querySelector('.status-text');
                if (label) label.textContent = text;
            });
        };

        fetch(url, { headers: { Accept: 'application/json' } })
            .then(res => res.json().then(data => ({ ok: res.ok, data })))
            .then(({ ok, data }) => {
                if (data.database !== 'connected') {
                    setState('down', 'Database unavailable');
                } else if (data.initialization === 'failed') {
                    setState('down', 'Database initialization failed');
                } else if (data.initialization === 'not_run') {
                    setState('down', 'Database connected');
                } else if (ok && data.status === 'ok' && data.initialization === 'initialized') {
                    setState('ok', 'Database connected · Startup initialization completed');
                } else {
                    setState('down', 'Database setup incomplete');
                }
            })
            .catch(() => setState('down', 'Status unavailable'));
    }

    function initComplaintAnalysis() {
        const textarea = document.getElementById('complaintText');
        if (!textarea || !textarea.dataset.analyzeUrl) return;

        const els = {
            state: document.getElementById('analysisState'),
            category: document.getElementById('resultCategory'),
            priority: document.getElementById('resultPriority'),
            department: document.getElementById('resultDepartment'),
            keywords: document.getElementById('resultKeywords'),
            reason: document.getElementById('resultReason'),
            review: document.getElementById('resultReview'),
        };
        let timer = null;
        let controller = null;

        const reset = () => {
            els.state.textContent = 'waiting for description';
            els.category.textContent = '—';
            els.priority.textContent = '—';
            els.department.textContent = '—';
            els.keywords.textContent = '—';
            if (els.reason) els.reason.textContent = 'The department explanation will appear as you describe the issue.';
            if (els.review) els.review.hidden = true;
        };

        const render = data => {
            els.state.textContent = 'analysis complete';
            els.category.textContent = data.category;
            els.department.textContent = data.department;
            if (els.reason) els.reason.textContent = data.reason || '';
            if (els.review) els.review.hidden = !data.needs_review;

            const badge = document.createElement('span');
            badge.className = 'priority ' + data.priority.toLowerCase();
            badge.textContent = data.priority;
            els.priority.replaceChildren(badge);

            if (data.keywords.length) {
                els.keywords.replaceChildren(...data.keywords.map(word => {
                    const chip = document.createElement('span');
                    chip.className = 'kw mono';
                    chip.textContent = word;
                    return chip;
                }));
            } else {
                els.keywords.textContent = 'No keywords matched — will be routed as Other';
            }
        };

        const analyze = () => {
            const text = textarea.value.trim();
            if (text.length < 3) { reset(); return; }
            if (controller) controller.abort();
            controller = new AbortController();
            els.state.textContent = 'analyzing…';

            fetch(textarea.dataset.analyzeUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text }),
                signal: controller.signal,
                credentials: 'same-origin',
            })
                .then(res => { if (!res.ok) throw new Error(res.status); return res.json(); })
                .then(render)
                .catch(err => { if (err.name !== 'AbortError') els.state.textContent = 'preview unavailable'; });
        };

        textarea.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(analyze, 350);
        });
        if (textarea.value.trim()) analyze();
    }

    function initLocationButton() {
        const button = document.getElementById('useLocationBtn');
        if (!button) return;
        const input = document.getElementById('location');
        const help = document.getElementById('locationHelp');

        button.addEventListener('click', () => {
            if (!navigator.geolocation) {
                help.textContent = 'Location is not supported by this browser. Please type the area.';
                return;
            }
            help.textContent = 'Getting your location…';
            navigator.geolocation.getCurrentPosition(
                position => {
                    const lat = position.coords.latitude.toFixed(6);
                    const lon = position.coords.longitude.toFixed(6);
                    input.value = `Latitude ${lat}, Longitude ${lon}`;
                    help.textContent = 'Location added. You can replace it with a landmark if needed.';
                },
                () => { help.textContent = 'Could not access location. Please type your area or landmark.'; }
            );
        });
    }

    function initImageInput() {
        const input = document.getElementById('imageInput');
        if (!input) return;
        const label = document.getElementById('imageLabel');
        const zone = input.closest('.dropzone');
        const defaultHtml = label.innerHTML;

        const show = () => {
            const file = input.files && input.files[0];
            if (file) {
                label.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
                zone.classList.add('has-file');
            } else {
                label.innerHTML = defaultHtml;
                zone.classList.remove('has-file');
            }
        };
        input.addEventListener('change', show);

        ['dragenter', 'dragover'].forEach(evt => zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.add('dragging'); }));
        ['dragleave', 'drop'].forEach(evt => zone.addEventListener(evt, e => { e.preventDefault(); zone.classList.remove('dragging'); }));
        zone.addEventListener('drop', e => {
            if (e.dataTransfer.files.length) {
                input.files = e.dataTransfer.files;
                show();
            }
        });
    }

    function initAutoSubmitSelects() {
        document.querySelectorAll('select[data-autosubmit]').forEach(select => {
            select.addEventListener('change', () => select.form.submit());
        });
    }

    function initTableFilters() {
        document.querySelectorAll('[data-table-filter]').forEach(input => {
            const table = document.querySelector(input.dataset.tableFilter);
            if (!table) return;
            input.addEventListener('input', () => {
                const query = input.value.trim().toLowerCase();
                table.querySelectorAll('tbody tr').forEach(row => {
                    row.hidden = query && !row.textContent.toLowerCase().includes(query);
                });
            });
        });
    }

    function initAmbientParallax() {
        const bg = document.querySelector('.ambient-bg');
        if (!bg) return;
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        if (!window.matchMedia('(pointer: fine)').matches) return;

        let frame = 0;
        let x = 0;
        let y = 0;
        window.addEventListener('pointermove', event => {
            x = event.clientX / window.innerWidth - 0.5;
            y = event.clientY / window.innerHeight - 0.5;
            if (frame) return;
            frame = requestAnimationFrame(() => {
                bg.style.setProperty('--px', x.toFixed(3));
                bg.style.setProperty('--py', y.toFixed(3));
                frame = 0;
            });
        }, { passive: true });
    }

    document.addEventListener('DOMContentLoaded', () => {
        initAmbientParallax();
        initHealthStatus();
        initComplaintAnalysis();
        initLocationButton();
        initImageInput();
        initAutoSubmitSelects();
        initTableFilters();
    });
})();
