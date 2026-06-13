from pydantic import BaseModel

class FileInfo(BaseModel):
    doc_title: str | None
    doc_summary: str | None