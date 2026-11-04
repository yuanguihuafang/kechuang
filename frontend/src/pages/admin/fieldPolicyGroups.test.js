import assert from 'node:assert/strict';
import test from 'node:test';

import { policyFieldGroups } from './fieldPolicyGroups.js';

test('shows separate external-redacted fields only for demand and enterprise', () => {
  const groups = policyFieldGroups({
    type: 'enterprise',
    public: ['enterprise_name', 'description', 'phone'],
    internal: ['created_by', 'business_scope'],
    system: ['id'],
  });

  assert.deepEqual(groups.map(({ key, title, fields }) => ({ key, title, fields })), [
    { key: 'public', title: '公开业务字段', fields: ['enterprise_name', 'description', 'phone'] },
    { key: 'internal', title: '外部用户脱敏字段', fields: ['created_by', 'business_scope'] },
    { key: 'system', title: '系统字段（不入向量）', fields: ['id'] },
  ]);

  const demandGroups = policyFieldGroups({
    type: 'demand',
    public: ['title', 'first_img_url', 'pub_dept', 'company'],
    internal: ['link_name', 'link_tel'],
    system: ['id'],
  });
  assert.deepEqual(demandGroups.map(({ key }) => key), ['public', 'internal', 'system']);
  assert.deepEqual(demandGroups[0].fields, ['title', 'first_img_url', 'pub_dept', 'company']);
  assert.ok(!demandGroups[1].fields.includes('first_img_url'));
  assert.deepEqual(demandGroups[1].fields, ['link_name', 'link_tel']);

  for (const type of ['achievement', 'expert', 'solution']) {
    const otherGroups = policyFieldGroups({
      type,
      public: ['title'],
      internal: ['phone'],
      system: ['id'],
    });
    assert.deepEqual(otherGroups.map(({ key }) => key), ['business', 'system']);
    assert.deepEqual(otherGroups[0].fields, ['title', 'phone']);
    assert.doesNotMatch(otherGroups[0].title, /外部用户隐藏/);
  }
});

test('labels identity-only fields separately from system fields', () => {
  const groups = policyFieldGroups({ type: 'customer_visit', identity_only: true, public: [], internal: [], system: ['user_id'] });

  assert.equal(groups[1].title, '身份字段（不入库）');
});
