"""
AFL Strategy Code Generator using Claude Code CLI.

Calls the Claude Code CLI directly (via subprocess) with the AFL strategy
development guide as context and a user-supplied strategy description,
returning generated AFL code.

Uses Claude Code subscription credits (not separate API credits).
"""

import json
import logging
import os
import re
import subprocess
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AFL guide (loaded once, cached)
# ---------------------------------------------------------------------------

_AFL_GUIDE_PATH = (
    Path(__file__).resolve().parent.parent / ".claude" / "agents" / "afl-strategy-guide.md"
)
_afl_guide_cache: str | None = None


def _load_afl_guide() -> str:
    """Load and cache the AFL strategy guide from disk."""
    global _afl_guide_cache
    if _afl_guide_cache is None:
        try:
            _afl_guide_cache = _AFL_GUIDE_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("AFL guide not found at %s", _AFL_GUIDE_PATH)
            _afl_guide_cache = ""
    return _afl_guide_cache


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(
    r"```(?:afl|c|clike|cpp)?\s*\n(.*?)```",
    re.DOTALL,
)

# Match XML tool_use wrapper: <content>...</content>
_XML_CONTENT_RE = re.compile(
    r"<content>(.*?)</content>",
    re.DOTALL,
)


def _extract_afl_code(response_text: str) -> str:
    """Extract AFL code from the CLI response.

    Handles several response formats:
    1. Fenced code block (```afl ... ```)
    2. XML tool_use wrapper (<content>...</content>)
    3. Raw AFL code (no wrapper)
    """
    # Try fenced code block first
    match = _CODE_FENCE_RE.search(response_text)
    if match:
        return match.group(1).strip()

    # Try XML <content>...</content> wrapper (from tool_use attempts)
    match = _XML_CONTENT_RE.search(response_text)
    if match:
        return match.group(1).strip()

    # Strip any leading non-AFL text (lines before the first comment or statement)
    lines = response_text.strip().splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("//") or "=" in stripped or stripped.startswith("Set"):
            start = i
            break
    if start > 0:
        return "\n".join(lines[start:]).strip()

    return response_text.strip()


# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

def _build_prompt(strategy_name: str, description: str, symbol: str) -> str:
    """Build the full prompt including AFL guide and strategy request."""
    guide = _load_afl_guide()

    rules = (
        "You are an AmiBroker AFL strategy code generator.\n\n"
        "RULES:\n"
        "- Return ONLY the raw AFL code text. Do NOT wrap it in XML, markdown fences,\n"
        "  tool_use blocks, or any other formatting. Just the plain AFL code.\n"
        "- Do NOT use any tools. Do NOT try to write files. Just output the code directly.\n"
        "- Follow the template and constraints in the guide EXACTLY.\n"
        "- NEVER use SetCustomBacktestProc, GetBacktesterObject, fopen, fputs, fclose, AddColumn, or Filter.\n"
        "- Use Param() for all tunable values.\n"
        "- Include SetTradeDelays(1,1,1,1) and trade on Open.\n"
        "- Include Buy, Sell, Short, Cover assignments (use 0 for unused directions).\n"
        "- Include ApplyStop for exits, SetPositionSize(1, spsShares), and ExRem.\n"
        "- Include visualization (Plot statements, PlotShapes for arrows, Title).\n"
    )
    if symbol:
        rules += f'- Use Name() == "{symbol}" for the symbol filter.\n'

    prompt = (
        f"{rules}\n"
        f"--- AFL STRATEGY DEVELOPMENT GUIDE ---\n\n"
        f"{guide}\n\n"
        f"--- STRATEGY REQUEST ---\n\n"
        f"Strategy Name: {strategy_name}\n"
    )
    if description:
        prompt += f"Description: {description}\n"
    if symbol:
        prompt += f"Symbol: {symbol}\n"
    prompt += (
        "\nGenerate the complete AFL code now. Return ONLY the code, "
        "no explanations or markdown."
    )
    return prompt


# ---------------------------------------------------------------------------
# Core generation function
# ---------------------------------------------------------------------------

def generate_afl(
    strategy_name: str,
    description: str,
    symbol: str = "",
) -> dict:
    """Call the Claude Code CLI to generate AFL code.

    Calls ``claude -p`` directly via subprocess, piping the prompt through
    stdin to avoid the Windows ~8191-char command-line argument limit.

    Returns a dict with keys:
        afl_code:  The generated AFL string
        warnings:  List of AFL validation warnings (may be empty)
        error:     Error string if generation failed, else None
        cost_usd:  Cost of the call, if available
    """
    user_prompt = _build_prompt(strategy_name, description, symbol)

    # Find the Claude CLI executable
    claude_cmd = shutil.which("claude")
    if not claude_cmd:
        return {
            "afl_code": "",
            "warnings": [],
            "error": (
                "Claude Code CLI not found in PATH. "
                "Ensure @anthropic-ai/claude-code is installed globally: "
                "npm install -g @anthropic-ai/claude-code"
            ),
            "cost_usd": None,
        }

    # Remove CLAUDE* env vars to avoid "nested session" block.
    # The Flask server runs inside a Claude Code session, so these
    # env vars would cause the child CLI process to refuse to start.
    clean_env = dict(os.environ)
    for key in list(clean_env):
        if key.startswith("CLAUDECODE") or key.startswith("CLAUDE_CODE"):
            del clean_env[key]

    # Call the CLI with --output-format stream-json so we can parse
    # structured output (assistant messages + result with cost).
    # The prompt is piped via stdin (the -p flag reads from stdin
    # when no inline prompt is given after --).
    # NOTE: --verbose is required for stream-json with --print.
    # --tools "" disables all tools so Claude generates AFL directly
    # instead of trying to read files or use other tools.
    cmd = [
        claude_cmd,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", "1",
        "--tools", "",
    ]

    logger.info("Calling Claude Code CLI: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            env=clean_env,
            timeout=180,
        )
    except FileNotFoundError:
        return {
            "afl_code": "",
            "warnings": [],
            "error": "Claude Code CLI not found. Ensure @anthropic-ai/claude-code is installed.",
            "cost_usd": None,
        }
    except subprocess.TimeoutExpired:
        return {
            "afl_code": "",
            "warnings": [],
            "error": "Claude Code CLI timed out after 3 minutes.",
            "cost_usd": None,
        }

    # Parse stream-json output.
    # Each line is a JSON object: assistant messages contain content,
    # result messages contain cost and error info.
    response_text = ""
    cost_usd = None

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type")
        if msg_type == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    response_text += block.get("text", "")
        elif msg_type == "result":
            cost_usd = msg.get("total_cost_usd")
            if msg.get("is_error"):
                return {
                    "afl_code": "",
                    "warnings": [],
                    "error": f"CLI error: {msg.get('result', 'Unknown error')}",
                    "cost_usd": cost_usd,
                }

    # If no structured output was found, try the raw stdout as plain text
    # (in case --output-format wasn't honored).
    if not response_text.strip() and result.stdout.strip():
        response_text = result.stdout.strip()

    if not response_text.strip():
        stderr_snippet = result.stderr[:500] if result.stderr else "(empty)"
        return {
            "afl_code": "",
            "warnings": [],
            "error": (
                f"Claude returned an empty response. "
                f"Exit code: {result.returncode}. "
                f"stderr: {stderr_snippet}"
            ),
            "cost_usd": cost_usd,
        }

    afl_code = _extract_afl_code(response_text)

    # Validate the generated AFL
    from scripts.afl_validator import validate_afl, auto_fix_afl

    ok, warnings = validate_afl(afl_code)
    if not ok:
        afl_code, fixes = auto_fix_afl(afl_code)
        if fixes:
            warnings = [f"Auto-fixed: {f}" for f in fixes] + warnings
            ok2, warnings2 = validate_afl(afl_code)
            if not ok2:
                warnings = warnings2

    return {
        "afl_code": afl_code,
        "warnings": warnings,
        "error": None,
        "cost_usd": cost_usd,
    }


# ---------------------------------------------------------------------------
# Iterative refinement
# ---------------------------------------------------------------------------

def _build_refinement_prompt(
    strategy_name: str,
    description: str,
    current_afl: str,
    backtest_results: dict,
    iteration: int,
    symbol: str = "",
) -> str:
    """Build a refinement prompt that includes backtest results feedback."""
    guide = _load_afl_guide()

    metrics = backtest_results.get("metrics", {})
    win_rate = metrics.get("win_rate", "N/A")
    net_profit = metrics.get("net_profit", "N/A")
    max_drawdown = metrics.get("max_drawdown", "N/A")
    num_trades = metrics.get("num_trades", "N/A")
    profit_factor = metrics.get("profit_factor", "N/A")
    avg_profit = metrics.get("avg_profit", "N/A")

    prompt = (
        "You are an AmiBroker AFL strategy code generator performing iterative refinement.\n\n"
        "RULES:\n"
        "- Return ONLY the raw AFL code text. No wrappers, no explanations.\n"
        "- Do NOT use any tools. Just output the code directly.\n"
        "- Follow the AFL development guide constraints.\n"
        "- NEVER use SetCustomBacktestProc, GetBacktesterObject, fopen, AddColumn, or Filter.\n"
        "- Use Param() for all tunable values.\n"
        "- Include SetTradeDelays(1,1,1,1) and trade on Open.\n\n"
        f"--- AFL STRATEGY DEVELOPMENT GUIDE ---\n\n{guide}\n\n"
        f"--- CURRENT STRATEGY (Iteration {iteration}) ---\n\n"
        f"Strategy Name: {strategy_name}\n"
        f"Description: {description}\n"
    )
    if symbol:
        prompt += f"Symbol: {symbol}\n"
    prompt += (
        f"\n--- CURRENT AFL CODE ---\n\n{current_afl}\n\n"
        f"--- BACKTEST RESULTS ---\n\n"
        f"Win Rate: {win_rate}\n"
        f"Net Profit: {net_profit}\n"
        f"Max Drawdown: {max_drawdown}\n"
        f"Number of Trades: {num_trades}\n"
        f"Profit Factor: {profit_factor}\n"
        f"Average Profit per Trade: {avg_profit}\n\n"
        f"--- REFINEMENT INSTRUCTIONS ---\n\n"
    )

    # Generate specific refinement guidance based on results
    instructions = []
    try:
        wr = float(str(win_rate).replace("%", ""))
        if wr < 40:
            instructions.append(
                "Win rate is below 40%. Make entry conditions more selective "
                "(add trend confirmation filters or tighter thresholds)."
            )
        elif wr > 70:
            instructions.append(
                "Win rate is high but check if trades are too infrequent. "
                "Consider loosening conditions slightly for more opportunities."
            )
    except (ValueError, TypeError):
        pass

    try:
        pf = float(str(profit_factor).replace(",", ""))
        if pf < 1.0:
            instructions.append(
                "Profit factor is below 1.0 (system is losing money). "
                "Improve exit logic: tighten stops, add trailing stops, "
                "or filter out low-quality signals."
            )
    except (ValueError, TypeError):
        pass

    try:
        nt = int(str(num_trades).replace(",", ""))
        if nt < 10:
            instructions.append(
                "Too few trades for statistical significance. "
                "Loosen entry conditions or reduce the lookback period."
            )
    except (ValueError, TypeError):
        pass

    if instructions:
        prompt += "\n".join(f"- {inst}" for inst in instructions)
    else:
        prompt += (
            "- Review the results and optimize the strategy.\n"
            "- Adjust entry/exit conditions to improve risk-adjusted returns.\n"
            "- Keep the core strategy logic but refine parameters and filters."
        )

    prompt += (
        "\n\nGenerate the improved AFL code now. Return ONLY the code, "
        "no explanations or markdown."
    )
    return prompt


def refine_afl(
    strategy_name: str,
    description: str,
    current_afl: str,
    backtest_results: dict,
    iteration: int = 1,
    symbol: str = "",
) -> dict:
    """Refine an AFL strategy based on backtest results.

    Takes the current AFL code and backtest results, then calls Claude
    to generate an improved version.

    Returns the same dict format as generate_afl().
    """
    prompt = _build_refinement_prompt(
        strategy_name, description, current_afl,
        backtest_results, iteration, symbol,
    )

    claude_cmd = shutil.which("claude")
    if not claude_cmd:
        return {
            "afl_code": current_afl,
            "warnings": ["Claude CLI not found — returning original AFL"],
            "error": "Claude Code CLI not found in PATH.",
            "cost_usd": None,
        }

    clean_env = dict(os.environ)
    for key in list(clean_env):
        if key.startswith("CLAUDECODE") or key.startswith("CLAUDE_CODE"):
            del clean_env[key]

    cmd = [
        claude_cmd, "-p",
        "--output-format", "stream-json",
        "--verbose", "--max-turns", "1", "--tools", "",
    ]

    logger.info("Calling Claude CLI for refinement (iteration %d)", iteration)

    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            env=clean_env, timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "afl_code": current_afl,
            "warnings": [f"Refinement failed: {exc}"],
            "error": str(exc),
            "cost_usd": None,
        }

    response_text = ""
    cost_usd = None

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_type = msg.get("type")
        if msg_type == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    response_text += block.get("text", "")
        elif msg_type == "result":
            cost_usd = msg.get("total_cost_usd")

    if not response_text.strip():
        return {
            "afl_code": current_afl,
            "warnings": ["Claude returned empty response — keeping original"],
            "error": "Empty response from Claude CLI",
            "cost_usd": cost_usd,
        }

    afl_code = _extract_afl_code(response_text)

    from scripts.afl_validator import validate_afl, auto_fix_afl
    ok, warnings = validate_afl(afl_code)
    if not ok:
        afl_code, fixes = auto_fix_afl(afl_code)
        if fixes:
            warnings = [f"Auto-fixed: {f}" for f in fixes] + warnings

    return {
        "afl_code": afl_code,
        "warnings": warnings,
        "error": None,
        "cost_usd": cost_usd,
    }


def iterative_generate(
    strategy_name: str,
    description: str,
    symbol: str = "",
    max_iterations: int = 3,
    backtest_callback=None,
) -> list[dict]:
    """Generate and iteratively refine an AFL strategy.

    Parameters
    ----------
    strategy_name : str
        Name of the strategy.
    description : str
        Strategy description for initial generation.
    symbol : str
        Target symbol.
    max_iterations : int
        Maximum refinement iterations (including initial generation).
    backtest_callback : callable or None
        A function that takes AFL code and returns backtest results dict.
        If None, only the initial generation is performed.

    Returns
    -------
    list[dict]
        List of dicts for each iteration with keys:
        iteration, afl_code, warnings, error, cost_usd, backtest_results.
    """
    results = []

    # Initial generation
    gen_result = generate_afl(strategy_name, description, symbol)
    iteration_result = {
        "iteration": 1,
        "afl_code": gen_result["afl_code"],
        "warnings": gen_result["warnings"],
        "error": gen_result["error"],
        "cost_usd": gen_result["cost_usd"],
        "backtest_results": None,
    }

    if gen_result["error"] or not gen_result["afl_code"]:
        results.append(iteration_result)
        return results

    # Run backtest if callback provided
    if backtest_callback:
        try:
            bt_results = backtest_callback(gen_result["afl_code"])
            iteration_result["backtest_results"] = bt_results
        except Exception as exc:
            logger.exception("Backtest callback failed on iteration 1: %s", exc)
            iteration_result["backtest_results"] = {"error": str(exc)}

    results.append(iteration_result)

    # Iterative refinement
    current_afl = gen_result["afl_code"]
    for i in range(2, max_iterations + 1):
        if not backtest_callback or not iteration_result.get("backtest_results"):
            break

        bt_results = iteration_result["backtest_results"]
        if bt_results.get("error"):
            break

        ref_result = refine_afl(
            strategy_name, description, current_afl,
            bt_results, iteration=i, symbol=symbol,
        )

        iteration_result = {
            "iteration": i,
            "afl_code": ref_result["afl_code"],
            "warnings": ref_result["warnings"],
            "error": ref_result["error"],
            "cost_usd": ref_result["cost_usd"],
            "backtest_results": None,
        }

        if ref_result["error"] or not ref_result["afl_code"]:
            results.append(iteration_result)
            break

        current_afl = ref_result["afl_code"]

        # Run backtest on refined version
        try:
            bt_results = backtest_callback(current_afl)
            iteration_result["backtest_results"] = bt_results
        except Exception as exc:
            logger.exception("Backtest callback failed on iteration %d: %s", i, exc)
            iteration_result["backtest_results"] = {"error": str(exc)}

        results.append(iteration_result)

    return results
