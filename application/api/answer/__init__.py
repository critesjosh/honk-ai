from flask import Blueprint

from application.api import api

# Importing each route module registers its Resource on ``answer_ns`` via
# the ``@answer_ns.route`` decorator — keep these imports even though the
# names are otherwise unused. Do NOT also ``api.add_resource`` them here:
# that registered every route twice.
from application.api.answer.routes.answer import AnswerResource  # noqa: F401
from application.api.answer.routes.base import answer_ns
from application.api.answer.routes.feedback import SubmitFeedback  # noqa: F401
from application.api.answer.routes.search import SearchResource  # noqa: F401
from application.api.answer.routes.stream import StreamResource  # noqa: F401
from application.api.answer.routes.version import VersionResource  # noqa: F401

answer = Blueprint("answer", __name__)

api.add_namespace(answer_ns)
