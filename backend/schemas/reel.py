"""Concept Reels: the closed visual vocabulary. The LLM fills these slots;
it never writes animation code (scenes are hand-drawn in features/render.py and
features/reel_engine.py)."""
from __future__ import annotations
from typing import Literal, Union
from pydantic import BaseModel, Field

ICON_NAMES = ("query", "document", "index", "encoder", "model", "database", "graph",
              "merge", "list", "user", "server", "browser", "cache", "lock", "cloud", "api")

class TitleSlots(BaseModel):
    title: str
    subtitle: str = ""

class DiagramNode(BaseModel):
    id: str
    label: str
    icon: str = ""             # flow only: one of ICON_NAMES draws the node as that object

class DiagramSlots(BaseModel):
    nodes: list[DiagramNode]
    edges: list[list[str]] = []
    order: list[str] = []

class CompareSide(BaseModel):
    title: str
    points: list[str]

class CompareSlots(BaseModel):
    left: CompareSide
    right: CompareSide

class TimelineSlots(BaseModel):
    steps: list[str] = Field(min_length=2, max_length=5)

class CodeSlots(BaseModel):
    lines: list[str] = Field(max_length=10)
    highlights: list[int] = []

class CounterSlots(BaseModel):
    from_: float = Field(alias="from")
    to: float
    label: str
    unit: str = ""
    model_config = {"populate_by_name": True}

class CalloutSlots(BaseModel):
    text: str

class Carry(BaseModel):
    """What visibly travels an edge: words, a number vector, a ranked list, a document."""
    kind: Literal["text", "vector", "list", "doc", "packet"]
    text: str = ""             # text: the words shown; vector: the text being encoded
    items: list[str] = Field(default=[], max_length=3)   # list: ranked items

class FlowEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    label: str = ""
    carry: Carry | None = None
    model_config = {"populate_by_name": True}

class FlowSlots(BaseModel):
    """Pipeline of objects: carries travel the edges and transform at each node.
    Edges may branch and merge; an edge pointing back is drawn as a loop arc."""
    nodes: list[DiagramNode] = Field(min_length=2, max_length=7)
    edges: list[FlowEdge] = Field(min_length=1, max_length=8)

class BarItem(BaseModel):
    label: str
    value: float

class BarChartSlots(BaseModel):
    """Animated score/ranking bars — quantities compared visually."""
    bars: list[BarItem] = Field(min_length=2, max_length=6)
    unit: str = ""
    highlight: str = ""        # label (or part of one) of the bar to emphasise

class TokenAttentionSlots(BaseModel):
    """A sentence of token chips; attention arcs fan out from the query token,
    arc thickness = weight — THE visual for attention/self-attention."""
    tokens: list[str] = Field(min_length=3, max_length=8)
    query: int = 0                       # index of the querying token
    weights: list[float] = []            # 0..1 per token (query's own weight ignored)

class MatrixHeatSlots(BaseModel):
    """Animated heatmap grid (attention patterns, score/confusion matrices)."""
    labels: list[str] = Field(min_length=2, max_length=6)   # shared row/col axes
    cells: list[list[float]] = []        # row-major 0..1; missing cells read as 0
    caption: str = ""                    # e.g. the formula the matrix comes from

class VectorPoint(BaseModel):
    label: str
    x: float                             # -1..1
    y: float                             # -1..1
    highlight: bool = False

class VectorSpaceSlots(BaseModel):
    """Labeled arrows in a 2D plane — embeddings, similarity, direction. With a
    query, its neighbors are emphasized, the rest dim, and the gap is measured."""
    vectors: list[VectorPoint] = Field(min_length=2, max_length=5)
    query: str = ""                      # label of the vector being searched with
    neighbors: list[str] = []            # labels of its nearest vectors

class KeywordMatchSlots(BaseModel):
    """Query words dropped onto a document and checked one by one — exact-term
    matching, and why a perfect page can score zero."""
    query: str                           # <= 4 words
    document: str                        # <= 10 words: the document's title or text
    score: float | None = None           # only when a fact states it
    score_label: str = ""                # e.g. "BM25 score"

class CycleSlots(BaseModel):
    """Stages with a dashed loop-back arrow — autoregressive/iterative processes."""
    steps: list[str] = Field(min_length=2, max_length=5)
    loop_label: str = ""                 # e.g. "append & repeat"

Slots = Union[TitleSlots, DiagramSlots, CompareSlots, TimelineSlots,
              CodeSlots, CounterSlots, CalloutSlots, FlowSlots, BarChartSlots,
              TokenAttentionSlots, MatrixHeatSlots, VectorSpaceSlots, CycleSlots,
              KeywordMatchSlots]

class Scene(BaseModel):
    archetype: Literal["title", "build_diagram", "compare_two", "timeline",
                       "code_reveal", "counter", "callout", "flow", "bar_chart",
                       "token_attention", "matrix_heat", "vector_space", "cycle",
                       "keyword_match"]
    slots: dict               # validated against the archetype's Slots model in validate_script
    narration: str            # <= 25 words
    grounded_in: str          # must reference an existing chunk_id

class SceneScript(BaseModel):
    title: str
    aspect: Literal["16:9"] = "16:9"
    scenes: list[Scene] = Field(min_length=1, max_length=8)   # 6-8 fills the minute
