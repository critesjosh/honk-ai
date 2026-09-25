"""POST /api/feedback — record a user's reaction to one assistant message.

Lives under the ``answer`` blueprint (not ``user/conversations`` like
upstream) because in this fork the only client that calls it is the
Discord bot's reaction-feedback flow. The admin SPA that owned the
upstream registration was removed.

URL is unchanged at ``/api/feedback`` so the bot's ``/api/feedback``
POST works without any client-side change. Auth is identical to the
upstream registration: anonymous → ``user_id="local"`` when
``AUTH_TYPE`` is unset (which is the prod config). If ``AUTH_TYPE``
is ever flipped to JWT mode, this route will start 401-ing for the
bot — matching upstream's behaviour exactly.
"""

import datetime

from flask import current_app, jsonify, make_response, request
from flask_restx import fields, Resource

from application.api import api
from application.api.answer.routes.base import answer_ns
from application.storage.db.repositories.conversations import ConversationsRepository
from application.storage.db.session import db_session
from application.utils import check_required_fields


@answer_ns.route("/api/feedback")
class SubmitFeedback(Resource):
    @api.expect(
        api.model(
            "FeedbackModel",
            {
                "question": fields.String(
                    required=False, description="The user question"
                ),
                "answer": fields.String(required=False, description="The AI answer"),
                "feedback": fields.String(required=True, description="User feedback"),
                "question_index": fields.Integer(
                    required=True,
                    description="The question number in that particular conversation",
                ),
                "conversation_id": fields.String(
                    required=True, description="id of the particular conversation"
                ),
                "api_key": fields.String(description="Optional API key"),
            },
        )
    )
    @api.doc(description="Submit feedback for a conversation")
    def post(self):
        decoded_token = request.decoded_token
        if not decoded_token:
            return make_response(jsonify({"success": False}), 401)
        data = request.get_json()
        required_fields = ["feedback", "conversation_id", "question_index"]
        missing_fields = check_required_fields(data, required_fields)
        if missing_fields:
            return missing_fields
        user_id = decoded_token.get("sub")
        feedback_value = data["feedback"]
        question_index = int(data["question_index"])
        # Normalize string feedback to lowercase so analytics queries
        # (which match 'like'/'dislike') count rows correctly. Tolerate
        # legacy uppercase clients on ingest. Non-string values pass through.
        if isinstance(feedback_value, str):
            feedback_value = feedback_value.lower()
        feedback_payload = (
            None
            if feedback_value is None
            else {
                "text": feedback_value,
                "timestamp": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
            }
        )
        try:
            with db_session() as conn:
                repo = ConversationsRepository(conn)
                conv = repo.get_any(data["conversation_id"], user_id)
                if conv is None:
                    return make_response(
                        jsonify({"success": False, "message": "Not found"}), 404
                    )
                repo.set_feedback(str(conv["id"]), question_index, feedback_payload)
        except Exception as err:
            current_app.logger.error(f"Error submitting feedback: {err}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)
        return make_response(jsonify({"success": True}), 200)
