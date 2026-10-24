from typing import Any

import pytest

from invoice_print_layout.reimbursement_people import ReimbursementPeople
from tests.test_material_matching import setup, proof


def test_receive_optional_person_is_validated_before_save(setup: Any) -> None:
    matcher, project, other = setup
    queue = matcher.intake
    people = ReimbursementPeople(matcher.store)
    person = people.create(project, 'Synthetic person')['person']
    payload = proof('new batch')
    for invalid in (people.create(other, 'Other person')['person']['id'], None, 'missing'):
        with pytest.raises(ValueError):
            queue.receive('proof.pdf', payload, project_id=project, reimbursement_person_id=invalid)
        assert queue.list() == []
        assert list(queue.folder.iterdir()) == []
    entry = queue.receive('proof.pdf', payload, project_id=project, reimbursement_person_id=person['id'])
    assert entry['reimbursement_person_id'] == person['id']
    assert entry['reimbursement_person_label'] == 'Synthetic person'
    assert queue.receive('again.pdf', payload, project_id=project)['reimbursement_person_id'] == person['id']
    assert queue.get(entry['id']) == entry
    unassigned = queue.receive('other.pdf', proof('without choice'), project_id=project)
    assert not unassigned.get('reimbursement_person_id')
