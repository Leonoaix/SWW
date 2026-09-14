"""Local PDF resume extraction and a deliberately explicit skill vocabulary."""

from __future__ import annotations

import io
import re
import unicodedata
from typing import Dict, List

from pypdf import PdfReader


MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_RESUME_CHARACTERS = 200_000

# Canonical names make spelling variants comparable without claiming that a
# related technology is interchangeable (Java is not JavaScript, SQL is not MySQL).
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


def normalize_text(text: str) -> str:
    """Normalize PDF ligatures and punctuation without erasing skill symbols."""
    return unicodedata.normalize("NFKC", text).replace("\x00", " ").replace("\u00ad", "")


def extract_skills(text: str) -> List[str]:
    """Return explicitly mentioned skills, not inferred qualifications."""
    normalized = normalize_text(text).casefold()
    found = []
    for skill, aliases in SKILL_ALIASES.items():
        if any(re.search(r"(?<![\w+#])" + re.escape(alias) + r"(?![\w+#])", normalized)
               for alias in aliases):
            found.append(skill)
    # One-letter languages require case and list-like context to avoid "a go",
    # the indefinite article, or the word "r" extracted from arbitrary prose.
    for name in ("C", "R", "Go"):
        if re.search(r"(?:^|[,:;/|\n])\s*" + name + r"\s*(?=$|[,:;/|\n])", normalize_text(text)):
            found.append(name)
    return sorted(set(found), key=str.casefold)


def extract_resume(data: bytes) -> Dict[str, object]:
    """Extract selectable PDF text locally; reject unsupported/unsafe inputs.

    Scans must be OCR'd before upload. This function does not send text to any
    service and deliberately rejects encrypted PDFs, even with empty passwords.
    """
    if not isinstance(data, bytes) or not data:
        raise ValueError("Upload a non-empty PDF resume.")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF resume must be 10 MiB or smaller.")
    if not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("The uploaded file is not a PDF.")

    warnings = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are unsupported. Export an unencrypted PDF first.")
        if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise ValueError("PDF resume must contain between 1 and 20 pages.")
        pages = []
        character_count = 0
        blank_pages = 0
        for page in reader.pages:
            page_text = normalize_text(page.extract_text() or "").strip()
            if not page_text:
                blank_pages += 1
            character_count += len(page_text)
            if character_count > MAX_RESUME_CHARACTERS:
                raise ValueError("PDF contains too much text for a resume (maximum 200,000 characters).")
            pages.append(page_text)
        text = "\n\n".join(pages).strip()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Could not read this PDF. Export a fresh PDF with selectable text.") from exc

    if not any(character.isalnum() for character in text):
        raise ValueError("No readable text found. Image-only/scanned PDFs need OCR before upload.")
    if blank_pages:
        warnings.append("%d page(s) contained no extractable text; check for scanned content." % blank_pages)
    if len(text) < 150:
        warnings.append("Very little resume text was extracted; check the preview before ranking.")
    if "\ufffd" in text:
        warnings.append("Some PDF characters could not be decoded; check the extracted text preview.")
    skills = extract_skills(text)
    if not skills:
        warnings.append("No skills from the local vocabulary were identified; ranking will rely on text and role overlap.")
    return {"text": text, "skills": skills, "warnings": warnings}
