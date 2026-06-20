"""
core.py — AssistantCore for the web version of Sky Assistant.

This is the SAME brain as the desktop sky_assistant.py build, carried over
almost untouched: process_text() in, reply string out, same calculator,
weather, reminders, notes, contacts, settings, and multi-turn pending-state
machine.

REMOVED vs. the desktop version (because these would control the SERVER's
machine, not the visitor's, which is meaningless/unsafe in a hosted app):
  - open_app()              (opened apps on the local Windows PC)
  - control_volume()        (PowerShell COM volume control)
  - control_brightness()    (screen_brightness_control)
  - open_chrome_search()    (launched a local Chrome window)
These intents now return a short explanation instead of trying to act.

Everything else — calculator, weather, reminders, notes, contacts, ask(),
settings customization, the PendingAction state machine — is unchanged in
behavior from the desktop build.
"""

import os
import re
import json
import math
import time
import random
import smtplib
import datetime
import threading
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests as req

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================
#   API KEYS  (set as environment variables on Render — never
#   sent to the browser)
# ============================================================

GROQ_API_KEY    = os.getenv("GROQ_API_KEY",    "your_groq_api_key_here")
NVIDIA_API_KEY  = os.getenv("NVIDIA_API_KEY",  "your_nvidia_api_key_here")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "your_openweathermap_key_here")
SENDER_EMAIL    = os.getenv("SENDER_EMAIL",    "youremail@gmail.com")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "your_gmail_app_password_here")

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


def missing_keys():
    missing = []
    if "your_groq" in GROQ_API_KEY:           missing.append("GROQ_API_KEY")
    if "your_nvidia" in NVIDIA_API_KEY:       missing.append("NVIDIA_API_KEY")
    if "your_openweather" in WEATHER_API_KEY: missing.append("WEATHER_API_KEY")
    if "youremail" in SENDER_EMAIL:           missing.append("SENDER_EMAIL")
    if "your_gmail" in SENDER_PASSWORD:       missing.append("SENDER_PASSWORD")
    return missing


# ============================================================
#   DEFAULT SETTINGS
#   NOTE: on Render's free tier the filesystem is ephemeral —
#   anything written to disk (settings, notes) is wiped on every
#   redeploy/restart. Fine for "try it with friends" use; revisit
#   with a real database later if you want this to persist.
# ============================================================

DEFAULT_SETTINGS = {
    "ai_name":       "Sky",
    "user_name":     "Boss",
    "voice_rate":    170,
    "wake_words":    ["hey sky", "hi sky", "sky", "wake up"],
    "default_city":  "Thrissur",
    "sleep_timeout": 600,
    "contacts": {
        "mum": "mum@gmail.com",
        "dad": "dad@gmail.com",
    }
}


# ============================================================
#   WORD NUMBERS / MATH HELPERS
# ============================================================

WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100, "thousand": 1000, "million": 1000000
}

MATH_WORDS = [
    "plus", "minus", "times", "divided", "multiplied", "into", "power",
    "root", "percent", "factorial", "sine", "cosine", "tangent", "log", "pi",
    "sin", "cos", "tan", "sqrt", "ln", "squared", "cubed", "half", "double",
    "triple", "mod", "modulo", "remainder", "raised", "negative", "subtract",
    "add", "multiply", "divide", "over", "+", "-", "*", "/"
]


def extract_number(text):
    if not text:
        return None
    match = re.search(r'(\d+)', text)
    if match:
        return int(match.group(1))
    for word, value in sorted(WORD_NUMBERS.items(), key=lambda x: len(x[0]), reverse=True):
        if word in text.lower():
            return value
    return None


def convert_word_numbers(text):
    operator_words = ["plus", "minus", "times", "into", "multiplied by", "divided by", "to the power of"]
    tokens = re.split(
        r'(\bplus\b|\bminus\b|\btimes\b|\binto\b|\bmultiplied by\b|\bdivided by\b|\bto the power of\b)',
        text
    )
    result = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token in operator_words:
            result.append(token)
        else:
            chunk = token
            for word, value in sorted(WORD_NUMBERS.items(), key=lambda x: len(x[0]), reverse=True):
                if word in chunk:
                    chunk = chunk.replace(word, str(value), 1)
            result.append(chunk)
    return " ".join(result)


def is_math_question(command):
    has_math = any(w in command for w in MATH_WORDS)
    has_digit = any(c.isdigit() for c in command)
    has_wordnum = any(w in command for w in WORD_NUMBERS)
    return has_math and (has_digit or has_wordnum)


# ============================================================
#   SAFE ARITHMETIC EVALUATOR  (no eval())
# ============================================================

class CalcError(Exception):
    pass


def safe_eval(expr: str) -> float:
    tokens = re.findall(r'\d+\.\d+|\d+|\*\*|[+\-*/()]', expr)
    if not tokens:
        raise CalcError("empty expression")
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def advance():
        tok = tokens[pos[0]]
        pos[0] += 1
        return tok

    def parse_expr():
        value = parse_term()
        while peek() in ("+", "-"):
            op = advance()
            rhs = parse_term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def parse_term():
        value = parse_factor()
        while peek() in ("*", "/"):
            op = advance()
            rhs = parse_factor()
            if op == "*":
                value = value * rhs
            else:
                if rhs == 0:
                    raise ZeroDivisionError()
                value = value / rhs
        return value

    def parse_factor():
        if peek() == "-":
            advance()
            return -parse_factor()
        if peek() == "+":
            advance()
            return parse_factor()
        return parse_power()

    def parse_power():
        base = parse_atom()
        if peek() == "**":
            advance()
            exponent = parse_factor()
            return base ** exponent
        return base

    def parse_atom():
        tok = peek()
        if tok is None:
            raise CalcError("unexpected end of expression")
        if tok == "(":
            advance()
            value = parse_expr()
            if peek() != ")":
                raise CalcError("missing closing parenthesis")
            advance()
            return value
        if re.match(r'^\d+\.\d+$|^\d+$', tok):
            advance()
            return float(tok)
        raise CalcError(f"unexpected token: {tok}")

    result = parse_expr()
    if pos[0] != len(tokens):
        raise CalcError("trailing tokens in expression")
    return result


# ============================================================
#   TEXT CLEANUP HELPERS
# ============================================================

def clean_ai_text(text):
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#+', '', text)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def strip_question_prefixes(query):
    q = query.lower().strip()
    prefixes = sorted([
        "who is", "who was", "who were", "who are",
        "what is", "what was", "what are", "what's",
        "what does", "what do", "where is", "where was",
        "when is", "when was", "when did", "how is",
        "how does", "how do", "how did", "how many", "how much",
        "why is", "why was", "why did",
        "tell me about", "tell me", "wikipedia", "define",
        "definition of", "look up", "find out about",
        "give me info on", "can you tell me about",
        "i want to know about", "explain to me", "explain",
        "full form of", "full form", "abbreviation of",
        "meaning of", "means", "stands for",
        "do you know about", "search for", "find",
        "get me info on", "info on", "about",
        "what do people think about", "feedback on",
        "opinions on", "news about", "latest news on",
        "latest news about", "what is happening with",
        "whats happening with", "latest on", "updates on"
    ], key=len, reverse=True)

    for phrase in prefixes:
        if q.startswith(phrase):
            q = q[len(phrase):].strip()
            break

    for article in ["the ", "a ", "an "]:
        if q.startswith(article):
            q = q[len(article):].strip()

    return q if q else query


def parse_time_string(time_str):
    try:
        t = time_str.strip().lower()
        period = "PM" if ("p.m" in t or t.endswith("pm") or " pm" in t) else "AM"
        for marker in ["p.m.", "a.m.", " pm", " am", "pm", "am", "p m", "a m"]:
            t = t.replace(marker, "")
        t = t.replace(".", "").replace(":", " ").strip()
        parts = t.split()
        if not parts or not parts[0].isdigit():
            return None
        if len(parts) == 1:
            num = parts[0]
            if len(num) <= 2:
                hour, minute = num.zfill(2), "00"
            elif len(num) == 3:
                hour, minute = num[0].zfill(2), num[1:]
            elif len(num) == 4:
                hour, minute = num[:2], num[2:]
            else:
                return None
        else:
            hour = parts[0].zfill(2)
            minute = parts[1].zfill(2) if parts[1].isdigit() else "00"
        return f"{hour}:{minute} {period}"
    except Exception:
        return None


# ============================================================
#   LLM LAYER — intent understanding + Q&A, unified request
#   helper with schema validation (same as desktop build)
# ============================================================

NLP_SYSTEM_PROMPT = """
You are the brain of a voice assistant.
The user will say something and you must figure out what they want.

Reply ONLY with a valid JSON object — nothing else.
No explanation, no preamble, no markdown. Just raw JSON.

JSON structure:
{
  "intent":     "<intent>",
  "contact":    "<name if emailing, else null>",
  "topic":      "<search topic or question, else null>",
  "city":       "<city name if weather, else null>",
  "task":       "<reminder or note content, else null>",
  "time":       "<reminder time e.g. 3 pm, else null>",
  "duration":   "<timer duration e.g. 5 minutes, else null>",
  "expression": "<math expression, else null>",
  "reply":      "<short friendly reply for small talk, else null>",
  "setting":    "<ai_name/user_name/speed/wake_word/city, else null>",
  "new_value":  "<the new value for the setting, else null>"
}

Valid intents:
- greeting, how_are_you, name, time, date, time_and_date
- reminder_set, reminder_show, timer, note_take, note_show
- send_email, add_contact, calculate, weather
- ask (ANY general knowledge: who is, what is, full form, explain, define, news, opinions, latest)
- change, joke, sleep, small_talk, unknown

IMPORTANT for the "setting" field:
- "change my name" / "call me" -> setting "user_name"
- "change your name" / "call you" -> setting "ai_name"
- "change speed" / "speak faster/slower" -> setting "speed"
- "change city" / "default city" -> setting "city"
- "change wake word" -> setting "wake_word"
"""

INTENT_SCHEMA = {
    "intent": "unknown", "contact": None, "topic": None, "city": None,
    "task": None, "time": None, "duration": None, "expression": None,
    "reply": None, "setting": None, "new_value": None,
}

VALID_INTENTS = {
    "greeting", "how_are_you", "name", "time", "date", "time_and_date",
    "reminder_set", "reminder_show", "timer", "note_take", "note_show",
    "send_email", "add_contact", "calculate", "weather", "ask",
    "change", "joke", "sleep", "small_talk", "unknown",
}


def validate_intent_json(raw_obj: dict) -> dict:
    result = dict(INTENT_SCHEMA)
    if not isinstance(raw_obj, dict):
        return result
    for key in INTENT_SCHEMA:
        if key in raw_obj:
            result[key] = raw_obj[key]
    if result["intent"] not in VALID_INTENTS:
        result["intent"] = "unknown"
    return result


def _call_llm(url, api_key, model, system_prompt, user_text,
               temperature=0.1, max_tokens=300, timeout=8):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    response = req.post(url, headers=headers, json=payload, timeout=timeout)
    data = response.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", str(data["error"])))
    if "choices" not in data or not data["choices"]:
        raise RuntimeError("no choices in response")
    return data["choices"][0]["message"]["content"].strip()


def understand_command(command: str) -> dict:
    try:
        raw = _call_llm(GROQ_URL, GROQ_API_KEY, "llama-3.3-70b-versatile",
                         NLP_SYSTEM_PROMPT, "User said: " + command)
        raw = raw.replace("```json", "").replace("```", "").strip()
        return validate_intent_json(json.loads(raw))
    except Exception as e:
        print(f"Groq intent failed: {e}")

    try:
        raw = _call_llm(NVIDIA_URL, NVIDIA_API_KEY, "meta/llama-3.3-70b-instruct",
                         NLP_SYSTEM_PROMPT, "User said: " + command)
        raw = raw.replace("```json", "").replace("```", "").strip()
        return validate_intent_json(json.loads(raw))
    except Exception as e:
        print(f"NVIDIA intent failed: {e}")

    return validate_intent_json(fallback_understand(command))


def fallback_understand(command):
    c = command.lower()
    knowledge = [
        "who is", "who was", "what is", "what was", "what are", "what's",
        "where is", "when is", "when was", "how does", "how many", "how much",
        "why is", "tell me about", "define", "full form", "abbreviation",
        "meaning of", "stands for", "explain", "look up", "find out",
        "what do people think", "feedback on", "opinions on",
        "news about", "latest news", "what is happening", "updates on"
    ]
    if any(t in c for t in knowledge):
        return {"intent": "ask", "topic": c}
    # NOTE: math check must come before the "time" check below, because
    # "times" (as in multiplication) contains the substring "time" and
    # would otherwise be misrouted to the time/date intent. This bug
    # existed in the original desktop script too.
    if "calculate" in c or is_math_question(c):
        return {"intent": "calculate", "expression": c}
    elif any(w in c for w in ["hello", "hi there", "hey"]):
        return {"intent": "greeting"}
    elif "how are you" in c:
        return {"intent": "how_are_you"}
    elif "your name" in c:
        return {"intent": "name"}
    elif "time" in c and "date" in c:
        return {"intent": "time_and_date"}
    elif "time" in c and "timer" not in c:
        return {"intent": "time"}
    elif "date" in c:
        return {"intent": "date"}
    elif ("show" in c or "list" in c) and "reminder" in c:
        return {"intent": "reminder_show"}
    elif "reminder" in c or "remind me" in c:
        return {"intent": "reminder_set", "task": None, "time": None}
    elif "timer" in c:
        return {"intent": "timer", "duration": c}
    elif ("show" in c or "read" in c) and "note" in c:
        return {"intent": "note_show"}
    elif any(p in c for p in ["take a note", "make a note", "note down", "save note"]):
        return {"intent": "note_take", "task": c}
    elif "email" in c or "send mail" in c:
        return {"intent": "send_email", "contact": None}
    elif "contact" in c and any(w in c for w in ["add", "save", "new", "create"]):
        return {"intent": "add_contact"}
    elif "weather" in c:
        city = c.split("in")[-1].strip() if "in" in c else None
        return {"intent": "weather", "city": city}
    elif any(w in c for w in ["change my name", "call me", "what should i call you"]):
        return {"intent": "change", "setting": "user_name", "new_value": None}
    elif any(w in c for w in ["change your name", "rename yourself"]):
        return {"intent": "change", "setting": "ai_name", "new_value": None}
    elif "change" in c:
        return {"intent": "change", "setting": None, "new_value": None}
    elif "joke" in c:
        return {"intent": "joke"}
    elif any(p in c for p in ["bye", "exit", "go to sleep", "goodbye"]):
        return {"intent": "sleep"}
    else:
        return {"intent": "unknown"}


def ask_llm_question(prompt: str):
    system = (
        "You are a helpful voice assistant. "
        "Answer in 2 clear natural spoken sentences. "
        "No markdown, no bullet points, no asterisks, no symbols, no numbering."
    )
    try:
        raw = _call_llm(GROQ_URL, GROQ_API_KEY, "llama-3.3-70b-versatile",
                         system, prompt, max_tokens=200, timeout=10)
        answer = clean_ai_text(raw)
        if answer and len(answer) > 10:
            return answer
    except Exception as e:
        print(f"Groq answer failed: {e}")

    try:
        raw = _call_llm(NVIDIA_URL, NVIDIA_API_KEY, "meta/llama-3.3-70b-instruct",
                         system, prompt, max_tokens=200, timeout=10)
        answer = clean_ai_text(raw)
        if answer and len(answer) > 10:
            return answer
    except Exception as e:
        print(f"NVIDIA answer failed: {e}")

    return None


# ============================================================
#   PENDING ACTION — multi-turn state machine (unchanged
#   concept from desktop build)
# ============================================================

@dataclass
class PendingAction:
    kind: str
    step: str
    data: dict = field(default_factory=dict)
    prompt: str = ""


# ============================================================
#   ASSISTANT CORE — one instance PER BROWSER SESSION.
#   The web app (app.py) creates a fresh AssistantCore for each
#   session id, so your "pending" state never crosses with a
#   friend's session using the same hosted app at the same time.
# ============================================================

class AssistantCore:
    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings["contacts"] = dict(DEFAULT_SETTINGS["contacts"])
        self.reminders = []
        self.notes = []          # in-memory, per session (ephemeral by design)
        self.last_query = ""
        self.last_shown_note = None
        self.pending: PendingAction | None = None

    # ---------- entry point ----------
    def process_text(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "I didn't catch that."

        if self.pending is not None:
            return self._continue_pending(text)

        understood = understand_command(text)
        intent = understood.get("intent", "unknown")
        return self._dispatch(intent, understood, text)

    # ---------- dispatch ----------
    def _dispatch(self, intent, u, raw_text) -> str:
        user_name = self.settings["user_name"]
        ai_name = self.settings["ai_name"]

        if intent == "greeting":
            return f"Hello {user_name}! How can I help you?"
        elif intent == "how_are_you":
            return f"I am doing great {user_name}, thank you for asking!"
        elif intent == "name":
            return f"My name is {ai_name}, your personal AI voice assistant!"
        elif intent == "time_and_date":
            now = datetime.datetime.now()
            return f"Today is {now.strftime('%B %d %Y')} and the time is {now.strftime('%I:%M %p')}."
        elif intent == "time":
            return f"The current time is {datetime.datetime.now().strftime('%I:%M %p')}."
        elif intent == "date":
            return f"Today is {datetime.datetime.now().strftime('%B %d %Y')}."
        elif intent == "reminder_set":
            return self.set_reminder(u.get("task"), u.get("time"))
        elif intent == "reminder_show":
            return self.show_reminders()
        elif intent == "timer":
            return self.set_timer(u.get("duration"))
        elif intent == "note_take":
            return self.take_note(u.get("task") or raw_text)
        elif intent == "note_show":
            return self.show_notes(raw_text)
        elif intent == "send_email":
            return self.start_send_email(u.get("contact"))
        elif intent == "add_contact":
            return self.start_add_contact()
        elif intent == "calculate":
            return self.calculate(u.get("expression") or raw_text)
        elif intent == "weather":
            return self.get_weather(u.get("city"))
        elif intent == "ask":
            return self.answer_question(u.get("topic") or raw_text)
        elif intent == "change":
            return self.start_customize(u.get("setting"), u.get("new_value"))
        elif intent == "joke":
            return random.choice([
                "Why do programmers prefer dark mode? Because light attracts bugs!",
                "Why did the computer go to the doctor? Because it had a virus!",
                "What do you call a computer that sings? A Dell!",
                "Why do Java developers wear glasses? Because they don't C sharp!",
                "How many programmers does it take to change a light bulb? None, that is a hardware problem!",
                "Why was the computer cold? It left its Windows open!",
                "Why did the programmer quit his job? Because he did not get arrays!",
                "What is a computer's favourite snack? Microchips!",
            ])
        elif intent == "small_talk":
            reply = u.get("reply")
            return clean_ai_text(reply) if reply else "That is interesting! Tell me more."
        elif intent == "sleep":
            return f"Okay, goodbye {user_name}!"
        else:
            return self.answer_question(raw_text)

    # ---------- pending multi-turn continuation ----------
    def _continue_pending(self, text: str) -> str:
        p = self.pending
        text = text.strip()

        if text.lower() in ("cancel", "never mind", "nevermind", "stop"):
            self.pending = None
            return "Okay, cancelled."

        if p.kind == "add_contact":
            if p.step == "name":
                p.data["name"] = text.lower()
                p.step = "email"
                p.prompt = f"What is {p.data['name']}'s email address?"
                return p.prompt
            elif p.step == "email":
                email = (text.strip().replace(" at ", "@").replace(" dot ", ".")
                         .replace(" underscore", "_").replace(" hyphen", "-")
                         .replace(" dash", "-").replace(" ", ""))
                p.data["email"] = email
                p.step = "confirm"
                p.prompt = f"Save {p.data['name']} with email {email}? Say yes or no."
                return p.prompt
            elif p.step == "confirm":
                self.pending = None
                if any(w in text.lower() for w in ["yes", "okay", "ok", "sure", "go ahead"]):
                    self.settings["contacts"][p.data["name"]] = p.data["email"]
                    return f"Done! {p.data['name']} is now saved in your contacts for this session."
                return "Contact not saved."

        elif p.kind == "send_email":
            if p.step == "contact":
                name, addr = self._find_contact(text)
                if not addr:
                    return "I couldn't find that person in your contacts. Try adding them first, or say cancel."
                p.data["name"], p.data["email"] = name, addr
                p.step = "subject"
                p.prompt = f"What is the subject of the email to {name}?"
                return p.prompt
            elif p.step == "subject":
                p.data["subject"] = text
                p.step = "body"
                p.prompt = "What should the email say?"
                return p.prompt
            elif p.step == "body":
                p.data["body"] = text
                p.step = "confirm"
                p.prompt = f"Sending email to {p.data['name']} with subject '{p.data['subject']}'. Say yes to confirm."
                return p.prompt
            elif p.step == "confirm":
                self.pending = None
                if any(w in text.lower() for w in ["yes", "send", "go", "okay", "ok"]):
                    return self._send_email_now(p.data["email"], p.data["name"], p.data["subject"], p.data["body"])
                return "Email cancelled!"

        elif p.kind == "change_setting":
            return self._continue_customize(text)

        self.pending = None
        return "Sorry, something went wrong with that conversation. Let's start over."

    def _find_contact(self, spoken_text: str):
        spoken_tokens = set(re.findall(r'\w+', spoken_text.lower()))
        for name, address in self.settings.get("contacts", {}).items():
            name_tokens = set(re.findall(r'\w+', name.lower()))
            if name_tokens & spoken_tokens:
                return name, address
        return None, None

    # ---------- reminders (in-memory per session) ----------
    def set_reminder(self, task, reminder_time):
        if not task:
            self.pending = PendingAction("reminder", "task", prompt="What would you like me to remind you about?")
            return self.pending.prompt
        if not reminder_time:
            self.pending = PendingAction("reminder", "time", data={"task": task},
                                          prompt=f"What time should I remind you to {task}?")
            return self.pending.prompt
        parsed = parse_time_string(reminder_time)
        if not parsed:
            return "Sorry, I could not understand that time. Try saying 3 PM or 10 30 AM."
        self.reminders.append({"task": task, "time": parsed.lower()})
        return f"Done! I will remind you to {task} at {parsed}. (Reminders only fire while this tab stays open.)"

    def show_reminders(self):
        if not self.reminders:
            return "You have no reminders set!"
        lines = [f"You have {len(self.reminders)} reminder{'s' if len(self.reminders) != 1 else ''}."]
        for i, r in enumerate(self.reminders):
            lines.append(f"Reminder {i + 1}: {r['task']} at {r['time']}.")
        return " ".join(lines)

    def due_reminders(self):
        now = datetime.datetime.now().strftime("%I:%M %p").lower()
        due = [r for r in self.reminders if r["time"] == now]
        for r in due:
            self.reminders.remove(r)
        return due

    # ---------- timers ----------
    def set_timer(self, duration_str):
        if not duration_str or not duration_str.strip():
            return "How long should I set the timer for? Try saying 'set timer 5 minutes'."
        duration_text = duration_str.lower().strip()
        number = extract_number(duration_text)
        if number is None:
            return "Sorry, I could not find a number. Try saying 5 minutes or ten seconds."
        if "second" in duration_text:
            seconds = number
        elif "minute" in duration_text:
            seconds = number * 60
        elif "hour" in duration_text:
            seconds = number * 3600
        else:
            return "Please say seconds, minutes, or hours."
        # Web version: timers are handled client-side via setTimeout once the
        # browser gets this reply (see static/app.js), since a background
        # Python thread on the server has no way to "speak" into a specific
        # visitor's tab later. This return value carries the seconds count
        # for the frontend to parse out.
        return f"TIMER:{seconds}:Timer started!"

    # ---------- notes (in-memory per session) ----------
    def take_note(self, task):
        note_text = (task or "")
        for phrase in ["take a note", "make a note", "note down", "save note", "remember"]:
            note_text = note_text.replace(phrase, "")
        note_text = note_text.strip()
        if not note_text:
            return "What would you like me to note down?"
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
        self.notes.append(f"[{timestamp}] {note_text}")
        return f"Got it! I have noted: {note_text} (kept for this session only)"

    def show_notes(self, query=None):
        if not self.notes:
            return "You have no notes saved yet!"
        total = len(self.notes)
        q = (query or "").lower()

        if any(w in q for w in ["latest", "last", "newest", "recent"]):
            self.last_shown_note = total - 1
            return f"Your latest note: {self.notes[self.last_shown_note]}"
        elif any(w in q for w in ["oldest", "first", "earliest"]):
            self.last_shown_note = 0
            return f"Your oldest note: {self.notes[0]}"
        elif any(w in q for w in ["all", "everything"]):
            count = min(5, total)
            out = [f"Here are your last {count} notes."]
            for i, line in enumerate(self.notes[-count:]):
                out.append(f"Note {total - count + i + 1}: {line}")
            self.last_shown_note = total - 1
            return " ".join(out)
        else:
            num = extract_number(q)
            if num is not None and 1 <= num <= total:
                self.last_shown_note = num - 1
                return f"Note {num}: {self.notes[self.last_shown_note]}"
            self.last_shown_note = total - 1
            return f"You have {total} note{'s' if total != 1 else ''}. Your latest note: {self.notes[self.last_shown_note]}."

    # ---------- email ----------
    def start_send_email(self, contact_name):
        if contact_name:
            name, addr = self._find_contact(contact_name)
            if addr:
                self.pending = PendingAction("send_email", "subject", data={"name": name, "email": addr},
                                              prompt=f"What is the subject of the email to {name}?")
                return self.pending.prompt
        self.pending = PendingAction("send_email", "contact", prompt="Who would you like to send the email to?")
        return self.pending.prompt

    def _send_email_now(self, recipient_email, recipient_name, subject, body):
        if "youremail" in SENDER_EMAIL or "your_gmail" in SENDER_PASSWORD:
            return "Email isn't configured on this server yet."
        try:
            msg = MIMEMultipart()
            msg["From"] = SENDER_EMAIL
            msg["To"] = recipient_email
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(SENDER_EMAIL, SENDER_PASSWORD)
                server.sendmail(SENDER_EMAIL, recipient_email, msg.as_string())
            return f"Email sent to {recipient_name} successfully!"
        except smtplib.SMTPAuthenticationError:
            return "Email authentication failed on the server side."
        except Exception as e:
            return f"Sorry, something went wrong with the email! ({e})"

    # ---------- contacts ----------
    def start_add_contact(self):
        self.pending = PendingAction("add_contact", "name", prompt="What is the contact's name?")
        return self.pending.prompt

    # ---------- calculator ----------
    def calculate(self, expression: str) -> str:
        try:
            for prefix in ["calculate", "compute", "solve", "what's", "what is",
                            "how much is", "how much", "tell me", "find", "give me"]:
                expression = expression.replace(prefix, "")
            expression = expression.replace(" the ", " ").strip()

            if not expression:
                return "What would you like me to calculate?"

            expression = convert_word_numbers(expression)
            expression = re.sub(r'(\d+)\s*point\s*(\d+)', r'\1.\2', expression)
            e = expression.strip()

            if e in ("pi", "value of pi"):
                return f"Pi is approximately {round(math.pi, 5)}"
            if e in ("e", "value of e", "euler", "eulers number"):
                return f"Euler's number is approximately {round(math.e, 5)}"
            if re.search(r'square root of|square root|sqrt', expression):
                n = float(re.sub(r'square root of|square root|sqrt', '', expression).strip())
                return f"Square root of {n} is {round(math.sqrt(n), 4)}"
            if re.search(r'cube root of|cube root', expression):
                n = float(re.sub(r'cube root of|cube root', '', expression).strip())
                return f"Cube root of {n} is {round(n ** (1 / 3), 4)}"
            if "squared" in expression:
                n = float(expression.replace("squared", "").strip())
                return f"{n} squared is {round(n ** 2, 4)}"
            if "cubed" in expression:
                n = float(expression.replace("cubed", "").strip())
                return f"{n} cubed is {round(n ** 3, 4)}"
            if "factorial" in expression:
                n = int(re.sub(r'factorial of|factorial', '', expression).strip())
                return f"Factorial of {n} is {math.factorial(n)}"
            if "percent of" in expression:
                parts = expression.split("percent of")
                pct, total = float(parts[0].strip()), float(parts[1].strip())
                return f"{pct} percent of {total} is {round((pct / 100) * total, 2)}"
            if re.search(r'\b(cosine of|cosine|cos)\b', expression):
                a = float(re.sub(r'\b(cosine of|cosine|cos)\b', '', expression).strip())
                return f"Cosine of {a} degrees is {round(math.cos(math.radians(a)), 4)}"
            if re.search(r'\b(sine of|sine|sin|sign)\b', expression):
                a = float(re.sub(r'\b(sine of|sine|sin|sign)\b', '', expression).strip())
                return f"Sine of {a} degrees is {round(math.sin(math.radians(a)), 4)}"
            if re.search(r'\b(tangent of|tangent|tan)\b', expression):
                a = float(re.sub(r'\b(tangent of|tangent|tan)\b', '', expression).strip())
                return f"Tangent of {a} degrees is {round(math.tan(math.radians(a)), 4)}"
            if re.search(r'natural log of|natural log|\bln\b', expression):
                n = float(re.sub(r'natural log of|natural log|\bln\b', '', expression).strip())
                return f"Natural log of {n} is {round(math.log(n), 4)}"
            if re.search(r'\b(log of|log)\b', expression):
                n = float(re.sub(r'\b(log of|log)\b', '', expression).strip())
                return f"Log of {n} is {round(math.log10(n), 4)}"
            if "half of" in expression:
                n = float(expression.replace("half of", "").strip())
                return f"Half of {n} is {round(n / 2, 4)}"
            if re.search(r'\bdouble\b', expression):
                n = float(re.sub(r'\bdouble\b', '', expression).strip())
                return f"Double of {n} is {round(n * 2, 4)}"
            if re.search(r'\btriple\b', expression):
                n = float(re.sub(r'\btriple\b', '', expression).strip())
                return f"Triple of {n} is {round(n * 3, 4)}"
            if re.search(r'\b(mod|modulo|remainder)\b', expression):
                clean = re.sub(r'remainder of|remainder when|remainder', '', expression)
                clean = re.sub(r'divided by|modulo|mod', 'MOD', clean)
                parts = clean.split('MOD')
                if len(parts) == 2:
                    a, b = float(parts[0].strip()), float(parts[1].strip())
                    return f"{int(a)} mod {int(b)} is {int(a % b)}"
            if "from" in expression and re.search(r'\b(subtract|take)\b', expression):
                clean = re.sub(r'\b(subtract|take|away)\b', '', expression)
                parts = clean.split("from")
                if len(parts) == 2:
                    a, b = float(parts[0].strip()), float(parts[1].strip())
                    return f"The answer is {round(b - a, 4)}"

            expression = (expression
                          .replace("multiplied by", "*").replace("multiply by", "*")
                          .replace("divided by", "/").replace("divide by", "/")
                          .replace("to the power of", "**").replace("to the power", "**")
                          .replace("raised to", "**").replace("power", "**")
                          .replace("plus", "+").replace("add", "+")
                          .replace("minus", "-").replace("subtract", "-")
                          .replace("times", "*").replace("multiply", "*")
                          .replace("into", "*").replace("divide", "/")
                          .replace("over", "/"))
            expression = re.sub(r'\bx\b', '*', expression)
            expression = re.sub(r'\bby\b', '/', expression)
            expression = re.sub(r'\band\b', '+', expression)
            expression = re.sub(r'\bnegative\b', '-', expression)
            expression = re.sub(r'[^0-9+\-*/.()\s]', '', expression)
            expression = re.sub(r'\s+', ' ', expression).strip()

            if not expression:
                return "Sorry, I could not understand the math expression!"

            result = round(safe_eval(expression), 4)
            if result == int(result):
                result = int(result)
            return f"The answer is {result}"

        except ZeroDivisionError:
            return "Sorry, you cannot divide by zero!"
        except CalcError as e:
            return f"Sorry, I could not parse that expression. ({e})"
        except Exception as e:
            return f"Sorry, I could not calculate that! ({e})"

    # ---------- weather ----------
    def get_weather(self, city):
        target = city or self.settings["default_city"]
        if "your_openweather" in WEATHER_API_KEY:
            return "Weather isn't configured on this server yet."
        try:
            response = req.get(
                "https://api.openweathermap.org/data/2.5/weather"
                f"?q={target}&appid={WEATHER_API_KEY}&units=metric",
                timeout=5
            )
            data = response.json()
            if data.get("cod") != 200:
                return f"Sorry, I could not find weather data for {target}."

            temp = round(data["main"]["temp"])
            feels_like = round(data["main"]["feels_like"])
            condition = data["weather"][0]["description"]
            main_weather = data["weather"][0]["main"].lower()
            humidity = data["main"]["humidity"]
            wind_speed = round(data.get("wind", {}).get("speed", 0) * 3.6)

            feels_part = f", though it feels more like {feels_like}" if abs(feels_like - temp) >= 2 else ""

            if wind_speed < 5:
                wind_part = "Winds are calm."
            elif wind_speed < 20:
                wind_part = f"A light breeze at {wind_speed} kilometres per hour."
            elif wind_speed < 40:
                wind_part = f"It is a bit windy at {wind_speed} kilometres per hour."
            else:
                wind_part = f"Strong winds at {wind_speed} kilometres per hour, be careful outside."

            if humidity > 80:
                humidity_part = f"Humidity is high at {humidity} percent, it might feel sticky."
            elif humidity > 60:
                humidity_part = f"Humidity is moderate at {humidity} percent."
            elif humidity > 30:
                humidity_part = f"Humidity is comfortable at {humidity} percent."
            else:
                humidity_part = f"The air is quite dry at {humidity} percent humidity."

            if "thunderstorm" in main_weather:
                advice = "There is a thunderstorm, best to stay indoors."
            elif "rain" in main_weather or "drizzle" in main_weather:
                advice = "You might want to grab an umbrella before heading out."
            elif "snow" in main_weather:
                advice = "It is snowing! Bundle up warm."
            elif any(w in main_weather for w in ["fog", "mist", "haze"]):
                advice = "Visibility is low, drive carefully."
            elif temp >= 38:
                advice = "It is extremely hot. Stay hydrated!"
            elif temp >= 33:
                advice = "Quite hot outside, drink plenty of water."
            elif temp >= 28:
                advice = "Nice and warm, great weather to be outdoors."
            elif temp >= 20:
                advice = "Pleasant weather, enjoy the day!"
            elif temp >= 10:
                advice = "A bit cool, a light jacket would be a good idea."
            elif temp >= 0:
                advice = "It is cold, dress warmly!"
            else:
                advice = "It is freezing out there, bundle up well!"

            return (f"It is currently {temp} degrees{feels_part}, with {condition}. "
                    f"{wind_part} {humidity_part} {advice}")
        except Exception as e:
            return f"Sorry, I could not get the weather right now! ({e})"

    # ---------- general questions ----------
    def answer_question(self, query):
        q = strip_question_prefixes(query)
        if not q:
            return "What would you like to know about?"
        self.last_query = q
        answer = ask_llm_question(q)
        if answer:
            return answer
        return f"Sorry, I could not find a good answer for '{q}' right now."

    # ---------- settings customization ----------
    def start_customize(self, setting, new_value):
        setting = (setting or "").lower()

        if any(w in setting for w in ["your name", "ai name", "assistant name", "ai_name"]):
            if new_value:
                return self._apply_ai_name(new_value)
            self.pending = PendingAction("change_setting", "ai_name",
                                          prompt=f"What would you like to call me instead of {self.settings['ai_name']}?")
            return self.pending.prompt

        elif any(w in setting for w in ["my name", "user name", "call me", "user_name"]):
            if new_value:
                return self._apply_user_name(new_value)
            self.pending = PendingAction("change_setting", "user_name",
                                          prompt=f"What should I call you instead of {self.settings['user_name']}?")
            return self.pending.prompt

        elif any(w in setting for w in ["speed", "rate", "faster", "slower"]):
            self.pending = PendingAction("change_setting", "speed", prompt="Say slow, normal, or fast.")
            return self.pending.prompt

        elif any(w in setting for w in ["city", "location", "default city"]):
            if new_value:
                return self._apply_city(new_value)
            self.pending = PendingAction("change_setting", "city",
                                          prompt="What city should I use as your default for weather?")
            return self.pending.prompt

        elif any(w in setting for w in ["wake word", "wake up word", "trigger word", "wake_word"]):
            if new_value:
                return self._apply_wake_word(new_value)
            self.pending = PendingAction("change_setting", "wake_word",
                                          prompt="What new wake word would you like to add?")
            return self.pending.prompt

        elif any(w in setting for w in ["settings", "show", "what are", "current"]):
            s = self.settings
            return (f"My name is {s['ai_name']}. I call you {s['user_name']}. "
                    f"Default city is {s['default_city']}. "
                    f"Wake words are {', '.join(s['wake_words'])}.")

        else:
            self.pending = PendingAction("change_setting", "which",
                                          prompt="What would you like to customize? Say my name, your name, speed, city, or wake word.")
            return self.pending.prompt

    def _continue_customize(self, text):
        p = self.pending
        self.pending = None
        text = text.strip()

        if p.step == "ai_name":
            return self._apply_ai_name(text)
        elif p.step == "user_name":
            return self._apply_user_name(text)
        elif p.step == "speed":
            t = text.lower()
            rate = 120 if "slow" in t else 190 if "fast" in t else 150
            self.settings["voice_rate"] = rate
            return f"Speed preference set. (Applies to the browser's voice settings.)"
        elif p.step == "city":
            return self._apply_city(text)
        elif p.step == "wake_word":
            return self._apply_wake_word(text)
        elif p.step == "which":
            return self.start_customize(text, None)
        return "Sorry, I lost track of that setting change."

    def _apply_ai_name(self, new_value):
        new_value = new_value.strip().title()
        old_name = self.settings["ai_name"]
        self.settings["ai_name"] = new_value
        self.settings["wake_words"] = [f"hey {new_value.lower()}", f"hi {new_value.lower()}",
                                        new_value.lower(), "wake up"]
        return f"Done! You can now call me {new_value} instead of {old_name}!"

    def _apply_user_name(self, new_value):
        self.settings["user_name"] = new_value.strip().title()
        return f"Got it! I will call you {self.settings['user_name']} from now on!"

    def _apply_city(self, new_value):
        self.settings["default_city"] = new_value.strip().title()
        return f"Default city set to {self.settings['default_city']}!"

    def _apply_wake_word(self, new_value):
        new_value = new_value.strip().lower()
        if new_value not in self.settings["wake_words"]:
            self.settings["wake_words"].append(new_value)
            return f"Done! You can now say {new_value} to wake me up!"
        return f"{new_value} is already a wake word!"
