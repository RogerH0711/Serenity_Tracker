import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline
from storage import connect_db, init_db, recent_pipeline_runs, start_pipeline_run


class PipelineRunnerTest(unittest.TestCase):
    def test_runs_each_stage_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "pipeline.lock"
            db_path = Path(directory) / "pipeline.db"
            init_db(db_path)
            connection = connect_db(db_path)
            try:
                start_pipeline_run(connection)
            finally:
                connection.close()
            with (
                patch.object(pipeline, "LOCK_PATH", lock_path),
                patch.object(pipeline, "DB_PATH", db_path),
                patch("pipeline.subprocess.run") as run,
            ):
                self.assertEqual(pipeline.main(), 0)
                self.assertEqual(run.call_count, len(pipeline.PIPELINE_SCRIPTS))
                called_scripts = [Path(call.args[0][1]).name for call in run.call_args_list]
                self.assertEqual(called_scripts, list(pipeline.PIPELINE_SCRIPTS))
            connection = connect_db(db_path)
            try:
                runs = recent_pipeline_runs(connection, 2)
                latest = runs[0]
                self.assertEqual(latest["status"], "success")
                self.assertEqual(runs[1]["status"], "failed")
                self.assertEqual(runs[1]["failure_kind"], "interrupted")
                self.assertEqual(runs[1]["error_message"], "前次程序在完成前中斷")
            finally:
                connection.close()

    def test_stops_after_failed_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "pipeline.lock"
            db_path = Path(directory) / "pipeline.db"
            failure = subprocess.CalledProcessError(7, ["python", "scraper.py"])
            with (
                patch.object(pipeline, "LOCK_PATH", lock_path),
                patch.object(pipeline, "DB_PATH", db_path),
                patch(
                    "pipeline.subprocess.run", side_effect=[None, failure]
                ) as run,
            ):
                self.assertEqual(pipeline.main(), 7)
                self.assertEqual(run.call_count, 2)
            connection = connect_db(db_path)
            try:
                latest = recent_pipeline_runs(connection, 1)[0]
                self.assertEqual(latest["status"], "failed")
                self.assertEqual(latest["failed_stage"], "scraper")
                self.assertEqual(latest["failure_kind"], "stage_failure")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
