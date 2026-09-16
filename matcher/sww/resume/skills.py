"""The explicit skill vocabulary, and how a claim earns the word "demonstrated".

Canonical names make spelling variants comparable without claiming that a
related technology is interchangeable (Java is not JavaScript, SQL is not
MySQL). The vocabulary is a *hint* for retrieval and for explaining a match —
it is no longer the thing the score is made of. The previous ranker awarded 60
of 100 points for "fraction of vocabulary terms in this posting that also
appear in the resume", so a posting describing the same work in different words
scored zero, and a posting naming one matching tool scored full marks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..text import clean_unicode
from .segment import Block, block_containing

SKILL_ALIASES = {
    "Python": ("python", "python3"),
    "Java": ("java",),
    "JavaScript": ("javascript", "ecmascript"),
    "TypeScript": ("typescript",),
    "C++": ("c++",),
    "C#": ("c#", "c sharp"),
    "C": ("c programming", "c language", "c/c++", "c, c++"),
    "Go": ("golang", "go programming", "go language"),
    "Rust": ("rust",),
    "Ruby": ("ruby",),
    "PHP": ("php",),
    "Swift": ("swift",),
    "Kotlin": ("kotlin",),
    "R": ("r programming", "r language", "r studio", "rstudio"),
    "MATLAB": ("matlab",),
    "SQL": ("sql",),
    "HTML": ("html", "html5"),
    "CSS": ("css", "css3"),
    "React": ("react", "reactjs", "react.js"),
    "Next.js": ("next.js", "nextjs"),
    "Vue": ("vue", "vue.js", "vuejs"),
    "Angular": ("angular",),
    "Node.js": ("node.js", "nodejs", "node js"),
    "Express": ("express.js", "expressjs", "express framework"),
    "Django": ("django",),
    "Flask": ("flask",),
    "FastAPI": ("fastapi",),
    "Spring": ("spring boot", "spring framework"),
    ".NET": (".net", "dotnet", "asp.net"),
    "REST APIs": ("rest api", "restful", "rest apis"),
    "GraphQL": ("graphql",),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "SQLite": ("sqlite",),
    "MongoDB": ("mongodb", "mongo db"),
    "Redis": ("redis",),
    "Git": ("git",),
    "Linux": ("linux",),
    "Bash": ("bash", "shell scripting"),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "AWS": ("aws", "amazon web services"),
    "Azure": ("azure",),
    "Google Cloud": ("gcp", "google cloud"),
    "Terraform": ("terraform",),
    "CI/CD": ("ci/cd", "ci cd", "continuous integration", "continuous delivery"),
    "Jenkins": ("jenkins",),
    "GitHub Actions": ("github actions",),
    "Unit testing": ("unit testing", "unit tests", "unittest"),
    "Pytest": ("pytest",),
    "Jest": ("jest",),
    "Playwright": ("playwright",),
    "Selenium": ("selenium",),
    "Cypress": ("cypress",),
    "Data structures": ("data structures",),
    "Algorithms": ("algorithms", "algorithm design"),
    "Distributed systems": ("distributed systems",),
    "Machine learning": ("machine learning",),
    "Deep learning": ("deep learning",),
    "PyTorch": ("pytorch",),
    "TensorFlow": ("tensorflow",),
    "Scikit-learn": ("scikit-learn", "sklearn", "scikit learn"),
    "Pandas": ("pandas",),
    "NumPy": ("numpy",),
    "Spark": ("apache spark", "pyspark"),
    "Hadoop": ("hadoop",),
    "Airflow": ("apache airflow", "airflow"),
    "Kafka": ("kafka",),
    "ETL": ("etl", "extract transform load"),
    "Data analysis": ("data analysis", "data analytics"),
    "Data visualization": ("data visualization", "data visualisation"),
    "Statistics": ("statistics", "statistical analysis", "statistical modeling"),
    "Tableau": ("tableau",),
    "Power BI": ("power bi", "powerbi"),
    "Excel": ("excel", "spreadsheets", "microsoft excel"),
    "VBA": ("vba", "visual basic for applications"),
    "Computer vision": ("computer vision", "opencv"),
    "NLP": ("nlp", "natural language processing"),
    "LLMs": ("llm", "llms", "large language model", "large language models"),
    "Cybersecurity": ("cybersecurity", "cyber security", "information security"),
    "Penetration testing": ("penetration testing", "pentesting"),
    "Networking": ("computer networks", "tcp/ip", "network protocols"),
    "Embedded systems": ("embedded systems", "embedded software", "firmware"),
    "RTOS": ("rtos", "real time operating system", "freertos"),
    "Verilog": ("verilog", "systemverilog"),
    "VHDL": ("vhdl",),
    "FPGA": ("fpga",),
    "PCB design": ("pcb design", "printed circuit board", "altium"),
    "Circuit design": ("circuit design", "analog circuits", "digital circuits"),
    "SolidWorks": ("solidworks", "solid works"),
    "AutoCAD": ("autocad",),
    "CAD": ("cad", "computer aided design", "computer-aided design"),
    "ANSYS": ("ansys",),
    "Finite element analysis": ("finite element analysis", "fea"),
    "Simulink": ("simulink",),
    "Robotics": ("robotics", "robot operating system", "ros2"),
    "Control systems": ("control systems", "control theory"),
    "Manufacturing": ("manufacturing",),
    "Lean Six Sigma": ("six sigma", "lean manufacturing"),
    "Quality assurance": ("quality assurance", "quality control"),
    "Project management": ("project management",),
    "Agile": ("agile", "scrum"),
    "Jira": ("jira",),
    "Figma": ("figma",),
    "UX research": ("ux research", "user research", "usability testing"),
    "UI design": ("ui design", "user interface design"),
    "UX design": ("ux design", "user experience design"),
    "Adobe Creative Suite": ("adobe creative suite", "adobe creative cloud"),
    "Photoshop": ("photoshop",),
    "Illustrator": ("adobe illustrator",),
    "Financial modeling": ("financial modeling", "financial modelling"),
    "Accounting": ("accounting",),
    "Valuation": ("valuation", "discounted cash flow"),
    "Financial analysis": ("financial analysis",),
    "Market research": ("market research",),
    "SEO": ("seo", "search engine optimization"),
    "Google Analytics": ("google analytics", "ga4"),
    "Digital marketing": ("digital marketing",),
    "Copywriting": ("copywriting",),
    "Salesforce": ("salesforce",),
    "SAP": ("sap",),
    "Supply chain": ("supply chain",),
    "Laboratory work": ("laboratory techniques", "lab techniques", "laboratory experience"),
    "PCR": ("pcr", "polymerase chain reaction"),
    "Cell culture": ("cell culture",),
}


# One-letter names need case and list-like context, or "a go" and the "r" in
# prose become skills.
_SHORT_NAMES = ("C", "R", "Go")
_SHORT_CONTEXT = {name: re.compile(r"(?:^|[,:;/|\n])\s*" + name + r"\s*(?=$|[,:;/|\n])") for name in _SHORT_NAMES}
_ALIAS_PATTERNS = {
    skill: [re.compile(r"(?<![\w+#])" + re.escape(alias) + r"(?![\w+#])") for alias in aliases]
    for skill, aliases in SKILL_ALIASES.items()
}


@dataclass(frozen=True)
class SkillMention:
    """Where a skill was found, and whether that location proves anything."""
    canonical: str
    alias: str
    start: int
    end: int
    block_id: Optional[str] = None
    demonstrated: bool = False


def extract_skills(text: str) -> list[str]:
    """Canonical names explicitly mentioned. Nothing is inferred."""
    normalized = clean_unicode(text).casefold()
    found = {skill for skill, patterns in _ALIAS_PATTERNS.items()
             if any(pattern.search(normalized) for pattern in patterns)}
    original = clean_unicode(text)
    found.update(name for name, pattern in _SHORT_CONTEXT.items() if pattern.search(original))
    return sorted(found, key=str.casefold)


def find_mentions(text: str, blocks: Optional[list[Block]] = None) -> list[SkillMention]:
    """Every occurrence with its offset, and the block that owns it.

    `demonstrated` is decided from the owning block's kind — a skill that only
    ever appears in a `Skills:` list is a claim, not evidence — so downstream
    code never has to re-scan raw text to work out what a citation means.
    """
    normalized = clean_unicode(text)
    folded = normalized.casefold()
    mentions: list[SkillMention] = []
    for skill, patterns in _ALIAS_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(folded):
                mentions.append(SkillMention(skill, match.group(), match.start(), match.end()))
    for name, pattern in _SHORT_CONTEXT.items():
        for match in pattern.finditer(normalized):
            mentions.append(SkillMention(name, name, match.start(), match.end()))
    if not blocks:
        return sorted(mentions, key=lambda item: (item.start, item.canonical))
    resolved = []
    for mention in mentions:
        block = next((b for b in blocks if b.start <= mention.start < b.end), None)
        resolved.append(SkillMention(mention.canonical, mention.alias, mention.start, mention.end,
                                     block.id if block else None, bool(block and block.is_evidence)))
    return sorted(resolved, key=lambda item: (item.start, item.canonical))


def demonstrated_skills(text: str, blocks: list[Block]) -> tuple[list[str], list[str]]:
    """Split mentions into (backed by work/projects, only listed)."""
    demonstrated, listed = set(), set()
    for mention in find_mentions(text, blocks):
        (demonstrated if mention.demonstrated else listed).add(mention.canonical)
    return sorted(demonstrated, key=str.casefold), sorted(listed - demonstrated, key=str.casefold)
