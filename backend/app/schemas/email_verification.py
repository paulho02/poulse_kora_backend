from pydantic import BaseModel


class EmailVerificationConfirm(BaseModel):
    code: str


class EmailVerificationStatus(BaseModel):
    is_verified: bool
