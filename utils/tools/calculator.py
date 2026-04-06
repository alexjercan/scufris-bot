"""Calculator tool for the agent."""

import logging

from langchain.tools import tool

logger = logging.getLogger("scufris-bot.tools.calculator")


@tool
def calculator_tool(expression: str) -> str:
    """
    Evaluate a mathematical expression and return the result.

    Supports basic arithmetic operations: +, -, *, /, **, %, and parentheses.

    Args:
        expression: A mathematical expression to evaluate (e.g., "2 + 2", "10 * 5 + 3")

    Returns:
        The result of the calculation as a string

    Examples:
        >>> calculator_tool("2 + 2")
        "4"
        >>> calculator_tool("10 * (5 + 3)")
        "80"
    """
    logger.info(f"📊 Calculating: {expression}")

    try:
        # Use eval with restricted globals/locals for safety
        # Only allow basic math operations and built-in math functions
        allowed_names = {
            "abs": abs,
            "round": round,
            "min": min,
            "max": max,
            "sum": sum,
            "pow": pow,
        }

        # Evaluate the expression
        result = eval(expression, {"__builtins__": {}}, allowed_names)

        logger.info(f"✓ Result: {result}")
        return str(result)
    except Exception as e:
        error_msg = f"Error evaluating expression: {str(e)}"
        logger.error(f"✗ {error_msg}")
        return error_msg
