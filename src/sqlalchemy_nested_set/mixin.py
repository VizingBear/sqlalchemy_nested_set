from sqlalchemy import Column, Integer


class NestedSetMixin:
    left = Column(Integer, nullable=False)
    right = Column(Integer, nullable=False)
