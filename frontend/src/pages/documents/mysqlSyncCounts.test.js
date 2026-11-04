import assert from 'node:assert/strict';
import test from 'node:test';
import { totalSyncCount } from './mysqlSyncCounts.js';

test('totals each knowledge type and ignores the aggregate error field', () => {
  const counts = {
    demand: { mysql_count: 7 },
    solution: { mysql_count: 5 },
    __error: 'not a knowledge type',
  };

  assert.equal(totalSyncCount(counts, 'mysql_count'), 12);
});

test('does not present an incomplete aggregate as a valid total', () => {
  const counts = {
    demand: { milvus_count: 4 },
    solution: { milvus_count: null },
  };

  assert.equal(totalSyncCount(counts, 'milvus_count'), null);
});
