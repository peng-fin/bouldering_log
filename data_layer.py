"""Data layer for the bouldering logger.

Storage is pluggable:
  - LocalStore:  reads/writes CSVs in a folder (dev + tests)
  - GitHubStore: reads/writes CSVs in a private GitHub repo (production on
                 Streamlit Community Cloud; every save is a commit)

All business logic (IDs, validation, derivation, milestones) lives in
BoulderingData and is storage-agnostic.
"""
from __future__ import annotations

import base64
import io
import os
from datetime import date as date_cls
from typing import Optional

import pandas as pd

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
SCHEMA = {
    "gyms": ["gym_id", "gym_name", "location", "notes"],
    "sessions": ["session_id", "date", "gym_id", "data_source",
                 "duration_min", "sleep_hours", "energy"],
    "problems": ["problem_id", "session_id", "problem_name", "grade", "style",
                 "movement_type", "hold_type", "attempts", "result",
                 "main_difficulty", "confidence", "project", "notes"],
    "attempts": ["attempt_id", "problem_id", "attempt_no", "result",
                 "fall_reason", "confidence", "notes"],
    "milestones": ["milestone_id", "type", "grade", "date", "gym_id",
                   "problem_id", "detail"],
}

GRADES = [f"V{i}" for i in range(0, 11)]
DATA_SOURCES = ("Live", "Historical")
RESULTS = ("Send", "Fall")


def grade_num(g: str) -> int:
    return int(str(g).lstrip("Vv"))


# --------------------------------------------------------------------------
# Storage backends
# --------------------------------------------------------------------------
class LocalStore:
    def __init__(self, folder: str = "bouldering_data"):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def read(self, name: str) -> pd.DataFrame:
        path = os.path.join(self.folder, f"{name}.csv")
        if not os.path.exists(path):
            return pd.DataFrame(columns=SCHEMA[name])
        return pd.read_csv(path, dtype=str, keep_default_na=False)

    def write(self, name: str, df: pd.DataFrame, message: str = "") -> None:
        df.to_csv(os.path.join(self.folder, f"{name}.csv"), index=False)


class GitHubStore:
    """Each write is one commit to `path_prefix/<name>.csv` on `branch`."""

    def __init__(self, token: str, repo: str, branch: str = "main",
                 path_prefix: str = "bouldering_data"):
        import requests  # local import so tests don't need it
        self._rq = requests
        self.repo, self.branch, self.prefix = repo, branch, path_prefix
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json"}
        self._sha: dict[str, str] = {}

    def _url(self, name: str) -> str:
        return (f"https://api.github.com/repos/{self.repo}/contents/"
                f"{self.prefix}/{name}.csv")

    def read(self, name: str) -> pd.DataFrame:
        r = self._rq.get(self._url(name), headers=self.headers,
                         params={"ref": self.branch}, timeout=15)
        if r.status_code == 404:
            self._sha.pop(name, None)
            return pd.DataFrame(columns=SCHEMA[name])
        r.raise_for_status()
        j = r.json()
        self._sha[name] = j["sha"]
        text = base64.b64decode(j["content"]).decode("utf-8")
        return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)

    def write(self, name: str, df: pd.DataFrame, message: str = "") -> None:
        body = {
            "message": message or f"Update {name}.csv",
            "content": base64.b64encode(
                df.to_csv(index=False).encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if name in self._sha:
            body["sha"] = self._sha[name]
        r = self._rq.put(self._url(name), headers=self.headers,
                         json=body, timeout=15)
        r.raise_for_status()
        self._sha[name] = r.json()["content"]["sha"]


# --------------------------------------------------------------------------
# Business logic
# --------------------------------------------------------------------------
class ValidationError(ValueError):
    pass


class BoulderingData:
    def __init__(self, store):
        self.store = store

    # ---- reads -----------------------------------------------------------
    def table(self, name: str) -> pd.DataFrame:
        df = self.store.read(name)
        for col in SCHEMA[name]:           # tolerate missing columns
            if col not in df.columns:
                df[col] = ""
        return df[SCHEMA[name]]

    # ---- ID generation ---------------------------------------------------
    @staticmethod
    def _next_id(existing: pd.Series, prefix: str, day: str) -> str:
        """prefix + YYYYMMDD + -NN, counting per date. day = 'YYYY-MM-DD'."""
        stamp = f"{prefix}{day.replace('-', '')}-"
        n = sum(1 for x in existing if str(x).startswith(stamp))
        return f"{stamp}{n + 1:02d}"

    @staticmethod
    def _next_gym_id(existing: pd.Series) -> str:
        nums = [int(x[1:]) for x in existing if str(x)[1:].isdigit()]
        return f"G{(max(nums) + 1 if nums else 1):03d}"

    @staticmethod
    def _day_of(id_: str) -> str:
        """'S20261008-01' -> '2026-10-08' (date is embedded in the ID)."""
        d = id_[1:9]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    # ---- gyms ------------------------------------------------------------
    def add_gym(self, gym_name: str, location: str = "", notes: str = "") -> str:
        gyms = self.table("gyms")
        if not gym_name.strip():
            raise ValidationError("gym_name is required")
        gid = self._next_gym_id(gyms["gym_id"])
        row = dict(gym_id=gid, gym_name=gym_name.strip(),
                   location=location, notes=notes)
        self.store.write("gyms", pd.concat([gyms, pd.DataFrame([row])],
                                           ignore_index=True),
                         f"Add gym {gid}")
        return gid

    # ---- sessions --------------------------------------------------------
    def add_session(self, date: str, gym_id: str, data_source: str,
                    duration_min: Optional[int] = None,
                    sleep_hours: Optional[float] = None,
                    energy: Optional[int] = None) -> str:
        if data_source not in DATA_SOURCES:
            raise ValidationError(f"data_source must be one of {DATA_SOURCES}")
        try:
            date_cls.fromisoformat(date)
        except ValueError:
            raise ValidationError("date must be YYYY-MM-DD")
        if gym_id not in set(self.table("gyms")["gym_id"]):
            raise ValidationError(f"unknown gym_id {gym_id}")
        if energy is not None and not 1 <= int(energy) <= 5:
            raise ValidationError("energy must be 1-5")
        if data_source == "Historical":    # per your sample: left blank
            duration_min = sleep_hours = energy = None

        sessions = self.table("sessions")
        sid = self._next_id(sessions["session_id"], "S", date)
        blank = lambda v: "" if v is None else str(v)
        row = dict(session_id=sid, date=date, gym_id=gym_id,
                   data_source=data_source, duration_min=blank(duration_min),
                   sleep_hours=blank(sleep_hours), energy=blank(energy))
        self.store.write("sessions",
                         pd.concat([sessions, pd.DataFrame([row])],
                                   ignore_index=True), f"Add session {sid}")
        return sid

    def set_session_duration(self, session_id: str, duration_min: int) -> None:
        sessions = self.table("sessions")
        mask = sessions["session_id"] == session_id
        if not mask.any():
            raise ValidationError(f"unknown session_id {session_id}")
        sessions.loc[mask, "duration_min"] = str(int(duration_min))
        self.store.write("sessions", sessions,
                         f"Set duration for {session_id}")

    # ---- problems --------------------------------------------------------
    def add_problem(self, session_id: str, grade: str, result: str,
                    problem_name: str = "", style: str = "",
                    movement_type: str = "", hold_type: str = "",
                    attempts: Optional[int] = None,
                    main_difficulty: str = "",
                    confidence: Optional[int] = None,
                    project: str = "", notes: str = "") -> str:
        sessions = self.table("sessions")
        if session_id not in set(sessions["session_id"]):
            raise ValidationError(f"unknown session_id {session_id}")
        if grade not in GRADES:
            raise ValidationError(f"grade must be one of {GRADES[0]}-{GRADES[-1]}")
        if result not in RESULTS:
            raise ValidationError(f"result must be one of {RESULTS}")
        if confidence is not None and not 1 <= int(confidence) <= 5:
            raise ValidationError("confidence must be 1-5")
        if attempts is not None and int(attempts) < 1:
            raise ValidationError("attempts must be >= 1")

        problems = self.table("problems")
        day = self._day_of(session_id)
        pid = self._next_id(problems["problem_id"], "P", day)
        blank = lambda v: "" if v is None else str(v)
        row = dict(problem_id=pid, session_id=session_id,
                   problem_name=problem_name, grade=grade, style=style,
                   movement_type=movement_type, hold_type=hold_type,
                   attempts=blank(attempts), result=result,
                   main_difficulty=main_difficulty,
                   confidence=blank(confidence), project=project, notes=notes)
        self.store.write("problems",
                         pd.concat([problems, pd.DataFrame([row])],
                                   ignore_index=True), f"Add problem {pid}")
        return pid

    # ---- attempts (Live only) -------------------------------------------
    def add_attempt(self, problem_id: str, result: str, fall_reason: str = "",
                    confidence: Optional[int] = None, notes: str = "") -> str:
        """Append the next attempt for a problem and re-derive the problem's
        `attempts` and `result` columns from its attempt rows."""
        problems = self.table("problems")
        match = problems[problems["problem_id"] == problem_id]
        if match.empty:
            raise ValidationError(f"unknown problem_id {problem_id}")
        sessions = self.table("sessions")
        sid = match.iloc[0]["session_id"]
        src = sessions.loc[sessions["session_id"] == sid, "data_source"].iloc[0]
        if src != "Live":
            raise ValidationError("attempt rows are only for Live sessions")
        if result not in RESULTS:
            raise ValidationError(f"result must be one of {RESULTS}")
        if result == "Send":
            fall_reason = ""               # no fall reason on a send

        attempts = self.table("attempts")
        mine = attempts[attempts["problem_id"] == problem_id]
        if (mine["result"] == "Send").any():
            raise ValidationError("problem already sent; no more attempts")
        n = len(mine) + 1
        aid = self._next_id(attempts["attempt_id"], "A", self._day_of(problem_id))
        row = dict(attempt_id=aid, problem_id=problem_id, attempt_no=str(n),
                   result=result, fall_reason=fall_reason,
                   confidence="" if confidence is None else str(confidence),
                   notes=notes)
        attempts = pd.concat([attempts, pd.DataFrame([row])], ignore_index=True)
        self.store.write("attempts", attempts, f"Add attempt {aid}")

        # derive problem-level fields from attempt rows
        problems.loc[problems["problem_id"] == problem_id, "attempts"] = str(n)
        problems.loc[problems["problem_id"] == problem_id, "result"] = result
        self.store.write("problems", problems,
                         f"Update {problem_id} from attempts")
        return aid

    # ---- joined view for metrics ----------------------------------------
    def problems_joined(self) -> pd.DataFrame:
        p, s, g = (self.table("problems"), self.table("sessions"),
                   self.table("gyms"))
        df = p.merge(s, on="session_id", how="left") \
              .merge(g, on="gym_id", how="left")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["grade_n"] = df["grade"].map(grade_num)
        df["attempts_n"] = pd.to_numeric(df["attempts"], errors="coerce")
        df["is_send"] = df["result"] == "Send"
        df["is_flash"] = df["is_send"] & (df["attempts_n"] == 1)
        return df

    # ---- milestones (generated) -----------------------------------------
    def generate_milestones(self) -> pd.DataFrame:
        df = self.problems_joined().dropna(subset=["date"]).sort_values(
            ["date", "problem_id"])
        rows = []

        def add(type_, r, detail=""):
            rows.append(dict(type=type_, grade=r["grade"],
                             date=r["date"].strftime("%Y-%m-%d"),
                             gym_id=r["gym_id"], problem_id=r["problem_id"],
                             detail=detail))

        sends = df[df["is_send"]]
        for _, r in sends.drop_duplicates("grade").iterrows():
            add("First send", r)
        for _, r in sends[sends["is_flash"]].drop_duplicates("grade").iterrows():
            add("First flash", r)

        best = -1                           # new max grade
        for _, r in sends.iterrows():
            if r["grade_n"] > best:
                best = r["grade_n"]
                add("New max grade", r)

        live = self.table("sessions")
        live = live[(live["data_source"] == "Live") &
                    (live["duration_min"] != "")]
        if not live.empty:
            live = live.assign(d=pd.to_numeric(live["duration_min"]))
            top = live.loc[live["d"].idxmax()]
            rows.append(dict(type="Longest session", grade="",
                             date=top["date"], gym_id=top["gym_id"],
                             problem_id="", detail=f"{int(top['d'])} min"))

        out = pd.DataFrame(rows, columns=SCHEMA["milestones"][1:])
        out.insert(0, "milestone_id",
                   [f"M{i + 1:03d}" for i in range(len(out))])
        return out[SCHEMA["milestones"]]

    def refresh_milestones(self) -> pd.DataFrame:
        m = self.generate_milestones()
        self.store.write("milestones", m, "Regenerate milestones")
        return m
