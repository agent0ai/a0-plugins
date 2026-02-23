const fs = require('fs');

async function run({ github, context }) {
  const marker = '<!-- a0-plugins-validation -->';
  const raw = fs.readFileSync('validation.log', 'utf8');
  const max = 60000;
  const text = raw.length > max ? raw.slice(0, max) + '\n... (truncated)\n' : raw;
  const body = `${marker}\n## Plugin submission validation failed\n\n\`\`\`\n${text}\n\`\`\`\n\nPush an update to this PR to re-run validation.`;

  const { owner, repo } = context.repo;
  const issue_number = context.payload.pull_request.number;

  const comments = await github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number,
    per_page: 100,
  });

  const existing = comments.find(
    (c) => (c.user?.type === 'Bot') && (c.body || '').includes(marker)
  );

  if (existing) {
    await github.rest.issues.updateComment({
      owner,
      repo,
      comment_id: existing.id,
      body,
    });
  } else {
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number,
      body,
    });
  }
}

module.exports = { run };
