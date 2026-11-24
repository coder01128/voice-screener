"""Interview script: loads config/interview.json and builds the interviewer prompt.

build_interviewer_prompt() and build_opening_line() are the only place the
interviewer's words are assembled. The voice bot and simulate.py both call
them; never copy their logic. All wording comes from the config file.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from screener.config import PROJECT_ROOT, ConfigError, read_json, require_str

INTERVIEW_CONFIG_PATH = PROJECT_ROOT / "config" / "interview.json"
MAX_FOLLOW_UPS = 2


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    question: str
    objective: str
    follow_ups: tuple[str, ...]
    optional: bool


@dataclass(frozen=True)
class PromptText:
    system_template: str
    topic_template: str
    optional_note: str
    no_follow_ups: str
    end_tool_name: str
    end_tool_description: str
    closing_line: str
    opening_template: str
    time_limit_goodbye: str


@dataclass(frozen=True)
class InterviewConfig:
    agent_name: str
    role_context: str
    max_call_seconds: int
    topics: tuple[Topic, ...]
    prompt: PromptText


def _load_topic(raw: dict, index: int, source: str) -> Topic:
    where = f"topics[{index}]"
    follow_ups = raw.get("follow_ups", [])
    if not isinstance(follow_ups, list) or not all(isinstance(f, str) and f.strip() for f in follow_ups):
        raise ConfigError(f"{source}: '{where}.follow_ups' must be a list of non-empty strings")
    if len(follow_ups) > MAX_FOLLOW_UPS:
        raise ConfigError(f"{source}: '{where}' has {len(follow_ups)} follow-ups; the maximum is {MAX_FOLLOW_UPS}")
    optional = raw.get("optional", False)
    if not isinstance(optional, bool):
        raise ConfigError(f"{source}: '{where}.optional' must be true or false")
    return Topic(
        id=require_str(raw, "id", f"{source} {where}"),
        title=require_str(raw, "title", f"{source} {where}"),
        question=require_str(raw, "question", f"{source} {where}"),
        objective=require_str(raw, "objective", f"{source} {where}"),
        follow_ups=tuple(f.strip() for f in follow_ups),
        optional=optional,
    )


def load_interview_config(path: Path = INTERVIEW_CONFIG_PATH) -> InterviewConfig:
    source = path.name
    data = read_json(path)

    agent_name = require_str(data, "agent_name", source)
    if "jem" in agent_name.lower():
        raise ConfigError(f"{source}: the agent must not be named after Jem; use 'Sam'")

    max_secs = data.get("max_call_seconds")
    if not isinstance(max_secs, int) or isinstance(max_secs, bool) or max_secs <= 0:
        raise ConfigError(f"{source}: 'max_call_seconds' must be a positive whole number")

    raw_topics = data.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ConfigError(f"{source}: 'topics' must be a non-empty list")
    topics = tuple(_load_topic(t, i, source) for i, t in enumerate(raw_topics))
    ids = [t.id for t in topics]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"{source}: topic ids must be unique")

    system_lines = data.get("prompt", {}).get("system_template")
    if not isinstance(system_lines, list) or not all(isinstance(line, str) for line in system_lines):
        raise ConfigError(f"{source}: 'prompt.system_template' must be a list of strings")

    prompt = PromptText(
        system_template="\n".join(system_lines),
        topic_template=require_str(data, "prompt.topic_template", source),
        optional_note=data["prompt"].get("optional_note", ""),
        no_follow_ups=require_str(data, "prompt.no_follow_ups", source),
        end_tool_name=require_str(data, "prompt.end_tool.name", source),
        end_tool_description=require_str(data, "prompt.end_tool.description", source),
        closing_line=require_str(data, "prompt.closing_line", source),
        opening_template=require_str(data, "prompt.opening_template", source),
        time_limit_goodbye=require_str(data, "prompt.time_limit_goodbye", source),
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", prompt.end_tool_name):
        raise ConfigError(f"{source}: 'prompt.end_tool.name' must be letters, digits and underscores only")

    config = InterviewConfig(
        agent_name=agent_name,
        role_context=require_str(data, "role_context", source),
        max_call_seconds=max_secs,
        topics=topics,
        prompt=prompt,
    )
    # Render once so a bad placeholder fails at load time, not mid-call.
    try:
        build_interviewer_prompt(config)
        build_opening_line(config)
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError(f"{source}: a prompt template has an unknown or broken placeholder: {e}") from e
    return config


def _render_topic(config: InterviewConfig, number: int, topic: Topic) -> str:
    p = config.prompt
    return p.topic_template.format(
        number=number,
        title=topic.title,
        optional_note=p.optional_note if topic.optional else "",
        question=topic.question,
        objective=topic.objective,
        follow_ups=" | ".join(topic.follow_ups) if topic.follow_ups else p.no_follow_ups,
    )


def build_interviewer_prompt(config: InterviewConfig) -> str:
    """The interviewer's system prompt. Shared by the voice bot and simulate.py."""
    topics = "\n".join(_render_topic(config, i, t) for i, t in enumerate(config.topics, start=1))
    return config.prompt.system_template.format(
        agent_name=config.agent_name,
        role_context=config.role_context,
        topics=topics,
        end_tool_name=config.prompt.end_tool_name,
    )


def build_opening_line(config: InterviewConfig) -> str:
    """What the interviewer says first: introduction, AI disclosure, first question."""
    return config.prompt.opening_template.format(
        agent_name=config.agent_name,
        first_question=config.topics[0].question,
    )
