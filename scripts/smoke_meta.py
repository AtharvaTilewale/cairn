"""Smoke test for the Meta Model API features Cairn depends on.
 
Checks, in order:
  1. a plain chat call to muse-spark-1.3
  2. structured output (strict JSON schema)
  3. image input: Muse Spark describes a matplotlib figure, like Cairn's figure reading
 
Reads MODEL_API_KEY from the environment or from a .env file in the current folder.
Never prints the key. Total cost is well under one US cent.
"""
 
import base64
import io
import json
import os
import sys
import time
 
from dotenv import load_dotenv
from openai import OpenAI
 
MODEL = "muse-spark-1.3"  # never the -contributor tier on real data
PRICE_IN, PRICE_OUT = 1.25, 4.25  # USD per million tokens
 
load_dotenv()
key = os.environ.get("MODEL_API_KEY")
if not key:
    sys.exit("MODEL_API_KEY not set. Put it in .env or export it.")
 
client = OpenAI(base_url="https://api.meta.ai/v1", api_key=key)
total_in = total_out = 0
 
 
def record(resp, label, started):
    global total_in, total_out
    u = resp.usage
    total_in += u.prompt_tokens
    total_out += u.completion_tokens
    print(f"  ok  {label}: {u.prompt_tokens} in / {u.completion_tokens} out, {time.time() - started:.1f}s")
 
 
def check(label, fn):
    try:
        fn()
    except Exception as e:  # report and continue so every check runs
        print(f"  FAIL {label}: {type(e).__name__}: {e}")
        return False
    return True
 
 
def plain():
    t = time.time()
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Reply with exactly: Cairn is live."}],
    )
    record(r, "plain chat", t)
    print(f"      reply: {r.choices[0].message.content.strip()}")
 
 
def structured():
    t = time.time()
    schema = {
        "type": "object",
        "properties": {
            "parameter": {"type": "string"},
            "old_value": {"type": "number"},
            "new_value": {"type": "number"},
        },
        "required": ["parameter", "old_value", "new_value"],
        "additionalProperties": False,
    }
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{
            "role": "user",
            "content": "In md.mdp the line 'dt = 0.002' was changed to 'dt = 0.001'. Extract the change.",
        }],
        response_format={"type": "json_schema", "json_schema": {"name": "Drift", "schema": schema, "strict": True}},
    )
    record(r, "structured output", t)
    data = json.loads(r.choices[0].message.content)
    print(f"      parsed: {data}")
    assert data["parameter"] == "dt" and data["new_value"] == 0.001, "unexpected values"
 
 
def vision():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
 
    ns = np.linspace(0, 50, 200)
    rmsd = 0.28 * (1 - np.exp(-ns / 6)) + 0.01 * np.sin(ns)
    fig, ax = plt.subplots(figsize=(6, 3.5), dpi=120)
    ax.plot(ns, rmsd)
    ax.set_xlabel("Time (ns)")
    ax.set_ylabel("RMSD (nm)")
    ax.set_title("Backbone RMSD")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    b64 = base64.b64encode(buf.getvalue()).decode()
 
    t = time.time()
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "In one sentence, as a lab notebook would: what does this plot show, "
                                     "and roughly when does it plateau and at what value?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
    )
    record(r, "figure reading", t)
    print(f"      reply: {r.choices[0].message.content.strip()}")
 
 
print(f"Meta Model API smoke test ({MODEL})")
results = [check("plain chat", plain), check("structured output", structured), check("figure reading", vision)]
cost = total_in / 1e6 * PRICE_IN + total_out / 1e6 * PRICE_OUT
print(f"\n{sum(results)}/3 passed. Tokens: {total_in} in, {total_out} out. Cost about ${cost:.4f}.")
sys.exit(0 if all(results) else 1)
