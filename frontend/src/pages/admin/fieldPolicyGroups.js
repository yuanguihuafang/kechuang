export function policyFieldGroups(policy) {
  const separateExternalFields = ['enterprise', 'demand'].includes(policy.type);

  if (!separateExternalFields) {
    return [
      { key: 'business', title: '业务字段（全部同步）', fields: [...policy.public, ...policy.internal] },
      {
        key: 'system',
        title: policy.identity_only ? '身份字段（不入库）' : '系统字段（不入向量）',
        fields: policy.system,
      },
    ];
  }

  return [
    { key: 'public', title: '公开业务字段', fields: policy.public },
    { key: 'internal', title: '外部用户脱敏字段', fields: policy.internal },
    {
      key: 'system',
      title: policy.identity_only ? '身份字段（不入库）' : '系统字段（不入向量）',
      fields: policy.system,
    },
  ];
}
