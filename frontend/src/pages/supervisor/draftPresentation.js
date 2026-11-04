export function normalizeSolutionDraftFields(
  fields = {},
  mysqlFields = {},
  identityCompany = "",
) {
  const publishedCompany =
    [
      fields.pub_dept,
      fields.company,
      mysqlFields.pub_dept,
      mysqlFields.company,
      identityCompany,
    ]
      .map((value) => String(value || "").trim())
      .find(Boolean) || "";
  const nextFields = { ...fields };
  const nextMysqlFields = { ...mysqlFields };
  delete nextFields.company;
  delete nextMysqlFields.company;
  if (publishedCompany) {
    nextFields.pub_dept = publishedCompany;
    nextMysqlFields.pub_dept = publishedCompany;
  }
  return { fields: nextFields, mysqlFields: nextMysqlFields };
}

export function projectDraftFields(fields, mysqlFields, allowedKeys) {
  const allowed = new Set(allowedKeys);
  return {
    fields: Object.fromEntries(
      Object.entries(fields).filter(([key]) => allowed.has(key)),
    ),
    mysqlFields: Object.fromEntries(
      Object.entries(mysqlFields).filter(([key]) => allowed.has(key)),
    ),
  };
}

export function detachDraftAttachment(draft, attachment) {
  const matchesAttachment = (item) => (
    attachment?.document_id
      ? item?.document_id === attachment.document_id
      : attachment?.storage_key
        ? item?.storage_key === attachment.storage_key
        : item === attachment
  );
  return {
    ...draft,
    attachments: (draft.attachments || []).filter((item) => !matchesAttachment(item)),
    source_blocks: [],
  };
}

export function updateDraftPublicationState(drafts, statuses, draftId, draftIndex, status) {
  const index = draftId
    ? drafts.findIndex((draft) => draft?.draft_id === draftId)
    : draftIndex;
  if (index < 0 || index >= drafts.length) return { drafts, statuses };
  return {
    drafts: drafts.map((draft, itemIndex) => (
      itemIndex === index ? { ...draft, status } : draft
    )),
    statuses: drafts.map((draft, itemIndex) => (
      itemIndex === index ? status : (statuses[itemIndex] || draft?.status || 'DRAFT_GENERATED')
    )),
  };
}

export function applyDraftOverrides(drafts, overrides) {
  return drafts.map((draft) => (
    draft?.draft_id && overrides[draft.draft_id]
      ? { ...draft, ...overrides[draft.draft_id] }
      : draft
  ));
}
