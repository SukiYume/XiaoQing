from __future__ import annotations

import pytest

from plugins.qingpet.services.database import Database
from plugins.qingpet.services.pet_service import PetService
from plugins.qingpet.utils.constants import (
    EVOLUTION_CONDITIONS,
    EVOLUTION_EVENTS_BY_STAGE,
    PetStage,
    validate_evolution_state_machine,
)
from tests.helpers.qingpet_test_support import fail_updates_to


@pytest.fixture
def pet_service(qingpet_db: Database):
    service = PetService(qingpet_db)
    service.adopt_pet("evolution-user", 10001, "岁岁")
    return service, qingpet_db


def _pet(database: Database):
    pet = database.get_pet("evolution-user", 10001)
    assert pet is not None
    return pet


def test_evolution_state_machine_covers_every_non_terminal_stage():
    validate_evolution_state_machine()
    assert set(EVOLUTION_EVENTS_BY_STAGE) == set(PetStage) - {PetStage.OLD}
    assert all(
        (stage, event) in EVOLUTION_CONDITIONS
        for stage, events in EVOLUTION_EVENTS_BY_STAGE.items()
        for event in events
    )


@pytest.mark.parametrize(
    ("age", "experience"),
    [
        (59, 150),
        (60, 149),
    ],
)
def test_mature_pet_requires_both_age_and_experience_boundaries(
    pet_service, age: int, experience: int
):
    service, database = pet_service
    pet            = _pet(database)
    pet.stage      = PetStage.MATURE
    pet.age        = age
    pet.experience = experience
    database.update_pet(pet)

    assert service.check_evolution(pet) == (False, "")
    persisted = _pet(database)
    assert persisted.stage == PetStage.MATURE
    assert persisted.experience == experience


def test_mature_pet_reaches_old_stage_exactly_once(pet_service):
    service, database = pet_service
    pet            = _pet(database)
    pet.stage      = PetStage.MATURE
    pet.form       = "成熟"
    pet.age        = 60
    pet.experience = 150
    database.update_pet(pet)
    version_before = _pet(database).version

    success, message = service.check_evolution(pet)

    assert success
    assert "老年" in message
    assert pet.stage == PetStage.OLD
    assert pet.form == "长寿"
    assert pet.experience == 0
    assert _pet(database).stage == PetStage.OLD
    assert service.check_evolution(pet) == (False, "")
    assert _pet(database).version == version_before + 1


@pytest.mark.parametrize(
    ("stage", "age", "experience", "stat", "expected_stage", "expected_form"),
    [
        (PetStage.YOUNG, 7, 50, 90, PetStage.GROWTH, "优秀"),
        (PetStage.YOUNG, 7, 50, 70, PetStage.GROWTH, "良好"),
        (PetStage.GROWTH, 21, 100, 20, PetStage.MATURE, "平凡"),
    ],
)
def test_young_and_growth_stats_choose_form_and_next_stage(
    pet_service,
    stage: PetStage,
    age: int,
    experience: int,
    stat: int,
    expected_stage: PetStage,
    expected_form: str,
):
    service, database = pet_service
    pet            = _pet(database)
    pet.stage      = stage
    pet.age        = age
    pet.experience = experience
    pet.hunger = pet.mood = pet.clean = pet.energy = pet.health = stat
    database.update_pet(pet)

    success, _message = service.check_evolution(pet)

    assert success
    assert pet.stage == expected_stage
    assert pet.form == expected_form


def test_failed_evolution_write_restores_in_memory_pet(pet_service):
    service, database = pet_service
    pet            = _pet(database)
    pet.stage      = PetStage.MATURE
    pet.form       = "成熟"
    pet.age        = 60
    pet.experience = 150
    database.update_pet(pet)
    fail_updates_to(database, "pets")

    assert service.check_evolution(pet) == (False, "")
    assert pet.stage == PetStage.MATURE
    assert pet.form == "成熟"
    assert pet.experience == 150
    persisted = _pet(database)
    assert persisted.stage == PetStage.MATURE
    assert persisted.experience == 150
