/* nexus-dl web UI - Alpine.js stores and SSE handling */

document.addEventListener('alpine:init', () => {

    /* Global app store */
    Alpine.data('appStore', () => ({
        modsDir: '',
        toast: { show: false, msg: '', type: 'info' },

        init() {
            // Restore any state if needed
        },

        showToast(msg, type = 'info') {
            this.toast = { show: true, msg, type };
            setTimeout(() => { this.toast.show = false; }, 4000);
        }
    }));

    /* Task store - handles background task execution + SSE progress */
    Alpine.data('taskStore', () => ({
        running: false,
        taskId: null,
        progress: 0,
        message: '',
        lastResult: null,
        eventSource: null,

        // Form state
        syncUrl: '',
        skipOptional: false,
        modUrl: '',
        fileId: '',
        localName: '',

        startSync() {
            this.startTask('/api/sync', {
                collection_url: this.syncUrl,
                skip_optional: this.skipOptional
            });
        },

        addMod() {
            const body = { mod_url: this.modUrl };
            if (this.fileId) body.file_id = parseInt(this.fileId);
            this.startTask('/api/add', body);
        },

        addLocal() {
            fetch('/api/add-local', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: this.localName })
            })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    this.showToast(data.error, 'error');
                } else {
                    this.showToast(`Registered "${data.name}" (ID: ${data.mod_id})`, 'success');
                    this.localName = '';
                    setTimeout(() => location.reload(), 1000);
                }
            })
            .catch(e => this.showToast(e.message, 'error'));
        },

        regenLoadOrder() {
            fetch('/api/load-order', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    this.showToast(data.error, 'error');
                } else {
                    this.showToast(`Load order regenerated: ${data.files.join(', ')}`, 'success');
                }
            })
            .catch(e => this.showToast(e.message, 'error'));
        },

        trackSync(action) {
            fetch(`/api/track-sync/${action}`, { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    this.showToast(data.error, 'error');
                } else if (data.status) {
                    this.showToast(`Track sync ${data.status}`, 'success');
                } else {
                    this.showToast(`Tracked: ${data.tracked}, Untracked: ${data.untracked}`, 'success');
                }
            })
            .catch(e => this.showToast(e.message, 'error'));
        },

        startTask(url, body) {
            this.running = true;
            this.progress = 0;
            this.message = 'Starting...';
            this.lastResult = null;

            if (this.eventSource) {
                this.eventSource.close();
                this.eventSource = null;
            }

            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            })
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    this.running = false;
                    this.showToast(data.error, 'error');
                    return;
                }
                this.taskId = data.task_id;
                this.connectSSE(data.task_id);
            })
            .catch(e => {
                this.running = false;
                this.showToast(e.message, 'error');
            });
        },

        connectSSE(taskId) {
            const es = new EventSource(`/api/tasks/${taskId}/stream`);
            this.eventSource = es;

            es.addEventListener('progress', (e) => {
                const data = JSON.parse(e.data);
                this.progress = data.pct || 0;
                this.message = data.msg || '';
            });

            es.addEventListener('status', (e) => {
                const data = JSON.parse(e.data);
                this.message = `Status: ${data.msg}`;
            });

            es.addEventListener('complete', (e) => {
                const data = JSON.parse(e.data);
                this.running = false;
                this.progress = 1;
                this.lastResult = { status: 'completed', data };
                this.showToast('Operation completed', 'success');
                es.close();
                this.eventSource = null;
            });

            es.addEventListener('error', (e) => {
                // SSE error event - check if it's a task error or connection error
                try {
                    const data = JSON.parse(e.data);
                    this.running = false;
                    this.lastResult = { status: 'failed', data };
                    this.showToast(data.msg || 'Operation failed', 'error');
                } catch {
                    // Connection error - poll for final status
                    this.pollTaskStatus(taskId);
                }
                es.close();
                this.eventSource = null;
            });
        },

        pollTaskStatus(taskId) {
            fetch(`/api/tasks/${taskId}`)
            .then(r => r.json())
            .then(data => {
                if (data.status === 'completed') {
                    this.running = false;
                    this.progress = 1;
                    this.lastResult = { status: 'completed', data: data.result };
                    this.showToast('Operation completed', 'success');
                } else if (data.status === 'failed') {
                    this.running = false;
                    this.lastResult = { status: 'failed', data: { msg: data.error } };
                    this.showToast(data.error || 'Operation failed', 'error');
                } else {
                    // Still running - retry
                    setTimeout(() => this.pollTaskStatus(taskId), 2000);
                }
            })
            .catch(() => {
                this.running = false;
                this.showToast('Lost connection to server', 'error');
            });
        },

        formatResult(result) {
            if (!result || !result.data) return '';
            try {
                return JSON.stringify(result.data, null, 2);
            } catch {
                return String(result.data);
            }
        },

        showToast(msg, type) {
            // Bubble up to appStore if available, otherwise just log
            const appEl = document.querySelector('[x-data="appStore"]');
            if (appEl && appEl.__x) {
                appEl.__x.$data.toast = { show: true, msg, type };
                setTimeout(() => { appEl.__x.$data.toast.show = false; }, 4000);
            } else {
                // Alpine 3 - use $dispatch or direct DOM access
                const body = document.body;
                if (body._x_dataStack) {
                    const store = body._x_dataStack[0];
                    if (store && store.toast) {
                        store.toast = { show: true, msg, type };
                        setTimeout(() => { store.toast.show = false; }, 4000);
                    }
                }
            }
        }
    }));
});
