"""Recal schema package – re-exports every public model so callers can write
``from ..schemas import Receipt, Card, …`` without knowing which sub-module
defines each class."""

from .common import Receipt, Citation
from .auth import (AuthRequest, AuthResponse, UserOut, FeedbackRequest,
                   FeedbackResponse, Connections, GraphStats)
from .calendar import Event, Slot, Move
from .chat import Action, ChatRequest
from .card import (ReelPayload, PagePayload, PlanPayload, PlanSession,
                   SourcePayload, DiffPayload, DocReviewPayload, ReviewIssue,
                   Card, Payload)
from .reel import (Scene, SceneScript, TitleSlots, DiagramSlots, CompareSlots,
                   TimelineSlots, CodeSlots, CounterSlots, CalloutSlots,
                   FlowSlots, BarChartSlots, TokenAttentionSlots,
                   MatrixHeatSlots, VectorSpaceSlots, CycleSlots,
                   KeywordMatchSlots, DiagramNode, FlowEdge, Carry, Slots)
from .conversation import Message, Conversation, CardRecord

