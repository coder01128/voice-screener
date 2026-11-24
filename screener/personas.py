"""Character cards for simulated calls (config/personas/<id>.json).

Only load_card() is for the persona-playing model, and it returns the `card`
section alone. A card file's `ground_truth` is never read here; the checker
reads it separately. The persona prompt wording lives in config/simulation.json.
"""

from dataclasses import dataclass
from pathlib import Path

from screener.config import PROJECT_ROOT, ConfigError, read_json, require_str

PERSONAS_DIR = PROJECT_ROOT / "config" / "personas"
SIMULATION_CONFIG_PATH = PROJECT_ROOT / "config" / "simulation.json"
NOT_PERSONA_FILES = {"expected.json"}


@dataclass(frozen=True)
class SimulationConfig:
    persona_model: str
    max_interviewer_turns: int
    persona_system_template: str
    card_line_template: str


def load_simulation_config(path: Path = SIMULATION_CONFIG_PATH) -> SimulationConfig:
    source = path.name
    data = read_json(path)
    max_turns = data.get("max_interviewer_turns")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 2:
        raise ConfigError(f"{source}: 'max_interviewer_turns' must be a whole number of at least 2")
    lines = data.get("persona_prompt", {}).get("system_template")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ConfigError(f"{source}: 'persona_prompt.system_template' must be a list of strings")
    config = SimulationConfig(
        persona_model=require_str(data, "persona_model", source),
        max_interviewer_turns=max_turns,
        persona_system_template="\n".join(lines),
        card_line_template=require_str(data, "persona_prompt.card_line_template", source),
    )
    try:
        build_persona_prompt({"name": "Test"}, config)
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(f"{source}: a persona prompt template has an unknown or broken placeholder: {e}") from e
    return config


def list_personas() -> list[str]:
    return sorted(p.stem for p in PERSONAS_DIR.glob("*.json") if p.name not in NOT_PERSONA_FILES)


def load_card(persona_id: str) -> dict:
    """The `card` section of a persona file: the only part the persona model may see."""
    path = PERSONAS_DIR / f"{persona_id}.json"
    if not path.exists():
        raise ConfigError(f"No persona '{persona_id}'. Available: {', '.join(list_personas())}")
    card = read_json(path).get("card")
    if not isinstance(card, dict) or not str(card.get("name", "")).strip():
        raise ConfigError(f"{path.name}: needs a 'card' section with a 'name'")
    return dict(card)


def build_persona_prompt(card: dict, config: SimulationConfig) -> str:
    lines = "\n".join(
        config.card_line_template.format(label=key.replace("_", " "), value=value)
        for key, value in card.items()
    )
    return config.persona_system_template.format(name=card["name"], card=lines)
