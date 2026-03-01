"""Flask application - routes for nexus-dl web UI."""

import json
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from ..api import NexusAPIError
from ..collection import CollectionParseError, ModParseError
from ..service import ModManagerService
from ..state import StateError
from .tasks import TaskManager


def create_app(api_key: str | None = None, mods_dir: Path | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["MODS_DIR"] = mods_dir
    app.config["API_KEY"] = api_key

    tasks = TaskManager()

    def get_service() -> ModManagerService:
        return ModManagerService(api_key=app.config["API_KEY"])

    def get_mods_dir() -> Path:
        return Path(app.config["MODS_DIR"])

    # -- Page routes --

    @app.route("/")
    def index():
        svc = get_service()
        mods_path = get_mods_dir()
        status = None
        error = None
        try:
            status = svc.get_status(mods_path)
        except StateError:
            error = "No collection synced yet. Use Sync to download a collection."
        except NexusAPIError as e:
            error = f"API error: {e}"
        except Exception as e:
            error = str(e)
        return render_template("index.html", status=status, error=error, mods_dir=str(mods_path))

    @app.route("/mods")
    def mods_page():
        svc = get_service()
        mods_path = get_mods_dir()
        status = None
        error = None
        try:
            status = svc.get_status(mods_path)
        except StateError:
            error = "No collection synced yet."
        except Exception as e:
            error = str(e)
        return render_template("mods.html", status=status, error=error, mods_dir=str(mods_path))

    # -- API routes --

    @app.route("/api/status")
    def api_status():
        svc = get_service()
        try:
            result = svc.get_status(get_mods_dir())
            return jsonify(asdict(result))
        except (StateError, NexusAPIError) as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/sync", methods=["POST"])
    def api_sync():
        data = request.get_json(silent=True) or {}
        collection_url = data.get("collection_url", "")
        skip_optional = data.get("skip_optional", False)
        no_extract = data.get("no_extract", False)

        if not collection_url:
            return jsonify({"error": "collection_url is required"}), 400

        task_id = tasks.create("sync")
        svc = get_service()
        mods_path = get_mods_dir()

        def progress_cb(event: str, pct: float, msg: str):
            tasks.update_progress(task_id, pct, msg)

        def run():
            return svc.sync(
                collection_url, mods_path,
                skip_optional=skip_optional,
                no_extract=no_extract,
                on_progress=progress_cb,
            )

        tasks.run_in_background(task_id, run)
        return jsonify({"task_id": task_id}), 202

    @app.route("/api/update", methods=["POST"])
    def api_update():
        data = request.get_json(silent=True) or {}
        skip_optional = data.get("skip_optional", False)
        no_extract = data.get("no_extract", False)

        task_id = tasks.create("update")
        svc = get_service()
        mods_path = get_mods_dir()

        def progress_cb(event: str, pct: float, msg: str):
            tasks.update_progress(task_id, pct, msg)

        def run():
            return svc.update(mods_path, skip_optional=skip_optional, no_extract=no_extract, on_progress=progress_cb)

        tasks.run_in_background(task_id, run)
        return jsonify({"task_id": task_id}), 202

    @app.route("/api/add", methods=["POST"])
    def api_add_mod():
        data = request.get_json(silent=True) or {}
        mod_url = data.get("mod_url", "")
        file_id = data.get("file_id")
        no_extract = data.get("no_extract", False)

        if not mod_url:
            return jsonify({"error": "mod_url is required"}), 400

        if file_id is not None:
            file_id = int(file_id)

        task_id = tasks.create("add_mod")
        svc = get_service()
        mods_path = get_mods_dir()

        def progress_cb(event: str, pct: float, msg: str):
            tasks.update_progress(task_id, pct, msg)

        def run():
            return svc.add_mod(mod_url, mods_path, file_id=file_id, no_extract=no_extract, on_progress=progress_cb)

        tasks.run_in_background(task_id, run)
        return jsonify({"task_id": task_id}), 202

    @app.route("/api/add-local", methods=["POST"])
    def api_add_local():
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400

        svc = get_service()
        try:
            mod_id = svc.add_local(name, get_mods_dir())
            return jsonify({"mod_id": mod_id, "name": name})
        except StateError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/deploy", methods=["POST"])
    def api_deploy():
        data = request.get_json(silent=True) or {}
        game_dir = data.get("game_dir")
        use_copy = data.get("use_copy", False)

        task_id = tasks.create("deploy")
        svc = get_service()
        mods_path = get_mods_dir()

        def progress_cb(event: str, pct: float, msg: str):
            tasks.update_progress(task_id, pct, msg)

        def run():
            gd = Path(game_dir) if game_dir else None
            return svc.deploy(mods_path, game_dir=gd, use_copy=use_copy, on_progress=progress_cb)

        tasks.run_in_background(task_id, run)
        return jsonify({"task_id": task_id}), 202

    @app.route("/api/undeploy", methods=["POST"])
    def api_undeploy():
        task_id = tasks.create("undeploy")
        svc = get_service()
        mods_path = get_mods_dir()

        def run():
            removed = svc.undeploy(mods_path)
            return {"removed": removed}

        tasks.run_in_background(task_id, run)
        return jsonify({"task_id": task_id}), 202

    @app.route("/api/load-order", methods=["POST"])
    def api_load_order():
        svc = get_service()
        try:
            files = svc.regenerate_load_order(get_mods_dir())
            return jsonify({"files": files})
        except StateError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/track-sync/<action>", methods=["POST"])
    def api_track_sync(action: str):
        svc = get_service()
        mods_path = get_mods_dir()
        try:
            if action == "enable":
                tracked, untracked = svc.track_sync_enable(mods_path)
                return jsonify({"tracked": tracked, "untracked": untracked})
            elif action == "disable":
                svc.track_sync_disable(mods_path)
                return jsonify({"status": "disabled"})
            elif action == "push":
                tracked, untracked = svc.track_sync_push(mods_path)
                return jsonify({"tracked": tracked, "untracked": untracked})
            else:
                return jsonify({"error": f"Unknown action: {action}"}), 400
        except (StateError, NexusAPIError) as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/tasks/<task_id>")
    def api_task_status(task_id: str):
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404

        result_data = None
        if task.result is not None:
            try:
                result_data = asdict(task.result)
            except (TypeError, Exception):
                result_data = task.result if isinstance(task.result, dict) else str(task.result)

        return jsonify({
            "id": task.id,
            "operation": task.operation,
            "status": task.status,
            "progress": task.progress,
            "message": task.message,
            "result": result_data,
            "error": task.error,
        })

    @app.route("/api/tasks/<task_id>/stream")
    def api_task_stream(task_id: str):
        return Response(
            tasks.stream_events(task_id),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
