"""
Session Resume - Save and restore interrupted batch creation sessions
"""
import os
import json
import logging
import tempfile
from datetime import datetime

logger = logging.getLogger('gmail_creator_session')

SESSION_FILE = "data/session_state.json"


class SessionManager:
    def __init__(self, filepath=SESSION_FILE):
        self.filepath = filepath

    def save_state(self, batch_config, completed_indices, results):
        """Save current batch progress for later resume."""
        state = {
            "saved_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "batch_config": batch_config,
            "completed_indices": completed_indices,
            "results": results,
        }
        directory = os.path.dirname(self.filepath) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".session-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(temporary, self.filepath)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        logger.info(f"Session saved: {len(completed_indices)} completed")

    def load_state(self):
        """Load saved session state. Returns None if no session found."""
        if not os.path.exists(self.filepath):
            return None
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.validate_state(state)
            logger.info(f"Session loaded from {state.get('saved_at', 'unknown')}")
            return state
        except Exception as e:
            logger.warning(f"Failed to load session: {e}")
            return None

    def clear_state(self):
        """Remove saved session file."""
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
            logger.info("Session state cleared")

    def has_saved_session(self):
        """Check if a saved session exists."""
        return os.path.exists(self.filepath)

    def get_remaining(self, state):
        """Get list of remaining account indices to create."""
        if state is None:
            return []
        self.validate_state(state)
        total = state["batch_config"].get("num_accounts", 0)
        completed = set(state.get("completed_indices", []))
        return [i for i in range(total) if i not in completed]

    @staticmethod
    def validate_state(state):
        if not isinstance(state, dict) or not isinstance(state.get("batch_config"), dict):
            raise ValueError("Invalid saved session: batch_config must be an object")
        total = state["batch_config"].get("num_accounts")
        if type(total) is not int or not 0 <= total <= 100:
            raise ValueError("Invalid saved session: num_accounts must be between 0 and 100")
        completed = state.get("completed_indices", [])
        if (not isinstance(completed, list)
                or any(type(index) is not int or not 0 <= index < total for index in completed)
                or len(set(completed)) != len(completed)):
            raise ValueError("Invalid saved session: completed_indices contains invalid indexes")
        results = state.get("results", {})
        if not isinstance(results, dict):
            raise ValueError("Invalid saved session: results must be an object")
        for key in ("successes", "failures"):
            value = results.get(key, 0)
            if type(value) is not int or not 0 <= value <= total:
                raise ValueError("Invalid saved session: invalid result counts")


session_manager = SessionManager()
