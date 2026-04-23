import os

TEST_DB_URL = "sqlite:///./test_suite.db"

os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["ENABLE_MOCK_CONNECTORS"] = "true"
os.environ["LLM_PROVIDER"] = "mock"

import pytest

from app.db import Base, engine


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

