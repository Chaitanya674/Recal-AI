from pydantic import BaseModel

class AuthRequest(BaseModel):
    id_token: str | None = None
    guest: bool = False

class UserOut(BaseModel):
    id: str
    name: str = "Guest"
    guest: bool = True

class AuthResponse(BaseModel):
    ok: bool
    user: UserOut

class FeedbackRequest(BaseModel):
    generation_id: str
    signal: str            # "up" | "down"
    note: str = ""

class FeedbackResponse(BaseModel):
    ok: bool
    style_descriptor: str | None = None

class Connections(BaseModel):
    calendar: bool = False
    gdocs: bool = False

class GraphStats(BaseModel):
    concepts: int = 0
    sources: int = 0
    chunks: int = 0
    edges: int = 0
