from pydantic import BaseModel, Field
from typing import List


class Message(BaseModel):
    role: str = Field(
        description="Message sender role: 'system', 'user', or 'assistant'.",
        examples=["system", "user", "assistant"],
    )
    content: str = Field(
        description="Text content of the message.",
        examples=["What is the capital of France?"],
    )


class PromptMessages(BaseModel):
    messages: List[Message] = Field(default_factory=list)


class ResponseChoice(BaseModel):
    message: Message


class ResponseChoices(BaseModel):
    choices: List[ResponseChoice] = Field(default_factory=list)


class PassAction(BaseModel):
    reason: str | None = Field(default=None)


class MaskAction(BaseModel):
    body: PromptMessages | ResponseChoices
    reason: str | None = Field(default=None)


class RejectAction(BaseModel):
    body: str
    status_code: int
    reason: str | None = Field(default=None)


class GuardrailsPromptRequest(BaseModel):
    body: PromptMessages


class GuardrailsPromptResponse(BaseModel):
    action: PassAction | MaskAction | RejectAction


class GuardrailsResponseRequest(BaseModel):
    body: ResponseChoices


class GuardrailsResponseResponse(BaseModel):
    action: PassAction | MaskAction
