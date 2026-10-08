"""Bouldering logger - phone-first Streamlit UI.

Run locally:   streamlit run app.py
Production:    Streamlit Community Cloud + [github] secrets (see README.md)
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from data_layer import (GRADES, BoulderingData, GitHubStore, LocalStore,
                        ValidationError)

TZ = ZoneInfo("Asia/Manila")   # Streamlit Cloud runs in UTC; keep dates local

# Dropdown choices. Starter lists - edit freely.
STYLES = ["Overhang", "Slab", "Vertical", "Roof", "Compression", "Arete"]
MOVEMENTS = ["Dynamic", "Static", "Coordination", "Balance", "Power"]
HOLDS = ["Crimp", "Sloper", "Jug", "Pinch", "Pocket", "Volume"]
DIFFICULTIES = ["Fear/Hesitation", "Reach", "Foot Slip", "Power", "Balance",
                "Beta", "Grip", "Other"]
FALL_REASONS = DIFFICULTIES

st.set_page_config(page_title="Boulder Log", page_icon="🧗", layout="centered")
st.markdown("""
<style>
.block-container {padding-top: 1.2rem; padding-bottom: 4rem;}
div.stButton > button {min-height: 3.2rem; font-size: 1.1rem;}
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
@st.cache_resource
def get_data() -> BoulderingData:
    try:
        gh = st.secrets["github"]
        store = GitHubStore(gh["token"], gh["repo"],
                            gh.get("branch", "main"),
                            gh.get("path_prefix", "bouldering_data"))
    except Exception:          # no secrets -> local folder (dev)
        store = LocalStore("bouldering_data")
    return BoulderingData(store)


data = get_data()
ss = st.session_state
ss.setdefault("sid", None)         # active session
ss.setdefault("pid", None)         # active Live problem
ss.setdefault("start_ts", None)
ss.setdefault("last_grade", "V3")


def pick(label, options, key=None):
    return st.selectbox(label, [""] + options, key=key,
                        format_func=lambda x: x or "-")


def guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValidationError as e:
        st.error(str(e))
    except Exception as e:     # storage/network failures
        st.error(f"Save failed: {e}")
    return None


# --------------------------------------------------------------------------
# Screen 1: start or resume a session
# --------------------------------------------------------------------------
def screen_start():
    st.title("🧗 Boulder Log")
    gyms = data.table("gyms")

    with st.expander("➕ Add a gym", expanded=gyms.empty):
        with st.form("new_gym", clear_on_submit=True):
            name = st.text_input("Gym name")
            loc = st.text_input("Location (optional)")
            if st.form_submit_button("Save gym", width="stretch"):
                if guard(data.add_gym, name, loc):
                    st.rerun()
    if gyms.empty:
        return

    st.subheader("New session")
    labels = dict(zip(gyms["gym_id"], gyms["gym_name"]))
    gym_id = st.selectbox("Gym", list(labels), format_func=labels.get)
    mode = st.radio("Type", ["Live", "Historical"], horizontal=True,
                    help="Live = logging as you climb. "
                         "Historical = entering a past session.")
    today = datetime.now(TZ).date()
    day = st.date_input("Date", value=today, max_value=today)
    sleep = energy = None
    if mode == "Live":
        sleep = st.number_input("Sleep last night (hours)", 0.0, 24.0,
                                value=None, step=0.5)
        energy = st.select_slider("Energy", [1, 2, 3, 4, 5], value=3)
    if st.button("Start session", type="primary", width="stretch"):
        sid = guard(data.add_session, day.isoformat(), gym_id, mode,
                    None, sleep, energy)
        if sid:
            ss.sid, ss.pid = sid, None
            ss.start_ts = datetime.now(TZ) if mode == "Live" else None
            st.rerun()

    sessions = data.table("sessions").tail(5).iloc[::-1]
    if not sessions.empty:
        st.subheader("Resume a recent session")
        for _, r in sessions.iterrows():
            gname = labels.get(r["gym_id"], r["gym_id"])
            if st.button(f"{r['date']} · {gname} · {r['data_source']}",
                         key=f"resume_{r['session_id']}",
                         width="stretch"):
                ss.sid, ss.pid, ss.start_ts = r["session_id"], None, None
                st.rerun()


# --------------------------------------------------------------------------
# Screen 2: inside a session
# --------------------------------------------------------------------------
def session_row():
    s = data.table("sessions")
    return s[s["session_id"] == ss.sid].iloc[0]


def end_session(srow):
    st.subheader("End session")
    default = None
    if srow["data_source"] == "Live":
        if ss.start_ts:
            default = max(1, int((datetime.now(TZ) - ss.start_ts).total_seconds() // 60))
        dur = st.number_input("Duration (min)", 1, 600, value=default, step=5)
        if st.button("Save & finish", type="primary", width="stretch"):
            if dur is not None:
                guard(data.set_session_duration, ss.sid, dur)
            ss.sid = ss.pid = ss.start_ts = None
            st.rerun()
    else:
        ss.sid = ss.pid = None
        st.rerun()


def problem_details():
    with st.expander("More details (optional)"):
        return dict(
            style=pick("Style", STYLES), movement_type=pick("Movement", MOVEMENTS),
            hold_type=pick("Hold type", HOLDS),
            main_difficulty=pick("Main difficulty", DIFFICULTIES),
            project=st.checkbox("Project (working it long-term)"),
            notes=st.text_input("Notes"),
        )


def screen_session():
    srow = session_row()
    gyms = data.table("gyms")
    gname = gyms.loc[gyms["gym_id"] == srow["gym_id"], "gym_name"].iloc[0]
    st.caption(f"{srow['data_source']} · {srow['date']} · {gname}")

    probs = data.table("problems")
    probs = probs[probs["session_id"] == ss.sid]
    sent = (probs["result"] == "Send").sum()
    st.markdown(f"### {sent} / {len(probs)} sent")

    if srow["data_source"] == "Live":
        live_panel()
    else:
        historical_form()

    if not probs.empty:
        st.dataframe(probs[["grade", "problem_name", "attempts", "result"]]
                     .iloc[::-1], hide_index=True, width="stretch")

    st.divider()
    if st.toggle("Finish this session"):
        end_session(srow)


def historical_form():
    with st.form("hist_problem", clear_on_submit=True):
        grade = st.selectbox("Grade", GRADES, index=GRADES.index(ss.last_grade))
        result = st.radio("Result", ["Send", "Fall"], horizontal=True)
        attempts = st.number_input("Attempts (optional)", 1, 99,
                                   value=None, step=1)
        name = st.text_input("Problem name (optional)")
        conf = st.selectbox("Confidence (optional)", [None, 1, 2, 3, 4, 5],
                            format_func=lambda x: "-" if x is None else str(x))
        extra = problem_details()
        if st.form_submit_button("Save problem", type="primary",
                                 width="stretch"):
            pid = guard(data.add_problem, ss.sid, grade, result,
                        problem_name=name, attempts=attempts,
                        confidence=conf,
                        project="Yes" if extra.pop("project") else "", **extra)
            if pid:
                ss.last_grade = grade
                st.toast(f"Saved {grade} {result}")
                st.rerun()


def live_panel():
    if ss.pid is None:
        with st.form("live_problem", clear_on_submit=True):
            grade = st.selectbox("Grade", GRADES,
                                 index=GRADES.index(ss.last_grade))
            name = st.text_input("Problem name (optional)")
            extra = problem_details()
            if st.form_submit_button("Start problem", type="primary",
                                     width="stretch"):
                # result starts as Fall; it is re-derived from attempts
                pid = guard(data.add_problem, ss.sid, grade, "Fall",
                            problem_name=name,
                            project="Yes" if extra.pop("project") else "",
                            **extra)
                if pid:
                    ss.pid, ss.last_grade = pid, grade
                    st.rerun()
        return

    probs = data.table("problems")
    prob = probs[probs["problem_id"] == ss.pid].iloc[0]
    att = data.table("attempts")
    att = att[att["problem_id"] == ss.pid]
    n = len(att)
    title = f"{prob['grade']} · {prob['problem_name'] or 'Unnamed'}"

    if (att["result"] == "Send").any():
        st.success(f"{title} - sent on attempt {n}"
                   + (" ⚡ flash!" if n == 1 else ""))
        if st.button("Next problem", type="primary", width="stretch"):
            ss.pid = None
            st.rerun()
        return

    st.markdown(f"#### {title} - attempt {n + 1}")
    k = f"{ss.pid}_{n}"                       # resets inputs after each tap
    reason = pick("If you fall, why?", FALL_REASONS, key=f"r_{k}")
    conf = st.select_slider("Confidence", [1, 2, 3, 4, 5], value=3,
                            key=f"c_{k}")
    note = st.text_input("Note (optional)", key=f"n_{k}")
    c1, c2 = st.columns(2)
    if c1.button("Fall", width="stretch", key=f"f_{k}"):
        if guard(data.add_attempt, ss.pid, "Fall", reason, conf, note):
            st.rerun()
    if c2.button("Send", type="primary", width="stretch",
                 key=f"s_{k}"):
        if guard(data.add_attempt, ss.pid, "Send", "", conf, note):
            st.rerun()
    if st.button("Give up on this problem", width="stretch"):
        ss.pid = None
        st.rerun()


# --------------------------------------------------------------------------
if ss.sid:
    screen_session()
else:
    screen_start()
