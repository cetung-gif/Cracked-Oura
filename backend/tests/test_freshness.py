from datetime import datetime, date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.src.models import Base, Sleep, HeartRate
from backend.src.sync import read_data_through


def test_data_through_comes_from_measurements_not_import_time():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert read_data_through(db) is None
        day = date.today() - timedelta(days=5)
        db.add(Sleep(id='sleep', day=day))
        db.commit()
        assert read_data_through(db) == day.isoformat()
        measured = datetime.now() - timedelta(days=2)
        db.add(HeartRate(timestamp=measured, bpm=60, source='test'))
        db.commit()
        assert read_data_through(db) == measured.date().isoformat()
