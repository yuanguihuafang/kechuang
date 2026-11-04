import { SupervisorDebug } from './SupervisorDebug.jsx';

export function SupervisorPage({ identity, testUsers, testUsersLoading, testUsersError, selectedTestUser, allowedScopes, onIdentityChange, knowledgeType, onKnowledgeTypeChange, onUploadAndParse, uploadBusy }) {
  return <SupervisorDebug
    key={identity.user_id || 'anonymous'}
    identity={identity}
    identityOptions={testUsers}
    identityLoading={testUsersLoading}
    identityError={testUsersError}
    selectedIdentity={selectedTestUser}
    allowedScopes={allowedScopes}
    onIdentityChange={onIdentityChange}
    knowledgeType={knowledgeType}
    onKnowledgeTypeChange={onKnowledgeTypeChange}
    onUploadAndParse={onUploadAndParse}
    uploadBusy={uploadBusy}
  />;
}
