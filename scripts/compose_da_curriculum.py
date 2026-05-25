"""Compose the data-analyst-curriculum scaffold from the bootcamp playbook."""
from skillmint.skill_synthesis import compose_skill_scaffold_from_playbook
import json

result = compose_skill_scaffold_from_playbook(
    playbook_name="data-analyst-bootcamp",
    skill_name="data-analyst-curriculum",
    trigger_description=(
        "Reference the full 28-hour data analyst curriculum from Alex The Analyst "
        "(2026 Bootcamp). Use when the user says 'what does Alex teach', 'show me "
        "the data analyst syllabus', 'what skills do I need to learn', 'where do I "
        "start as a data analyst', or asks for the canonical learning path covering "
        "SQL, Excel, Tableau, Power BI, Python, Pandas, R, Git/GitHub, AWS, Azure, "
        "Databricks, portfolio building, resume writing, and job hunting. Acts as "
        "the syllabus reference for the data-analyst agent."
    ),
    scope_notes=(
        "This is a curriculum REFERENCE skill - it points at the captured playbook "
        "so the data-analyst agent can cite section/timestamp when explaining what "
        "to learn next. It does NOT contain the procedures themselves - those live "
        "in individual skills (sql-tsql, power-bi, excel-analyst, python-pandas, "
        "etc.) that this curriculum maps onto."
    ),
    overwrite=True,
    skills_root=r"C:\Users\eddyo\Github\PeripheryAI\Periphery\.claude\skills",
)
print(json.dumps({
    "skill_path": result.get("skillPath"),
    "sections": result.get("sectionCount"),
    "words": result.get("wordCount"),
}, indent=2, default=str))
