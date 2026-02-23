const fs = require('fs');

async function run({ github, context }) {
  const marker = '<!-- a0-plugins-validation -->';
  const raw = fs.readFileSync('validation.log', 'utf8');
  const max = 60000;
  const text = raw.length > max ? raw.slice(0, max) + '\n... (truncated)\n' : raw;
  const body = `${marker}\n## ❌ Plugin submission validation failed\n\n\`\`\`\n${text}\n\`\`\`\n\nPush an update to this PR to re-run validation.`;

  const { owner, repo } = context.repo;
  const issue_number = context.payload.pull_request.number;

  console.log(`Commenting on PR #${issue_number} in ${owner}/${repo}`);

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
    const res = await github.rest.issues.updateComment({
      owner,
      repo,
      comment_id: existing.id,
      body,
    });
    console.log(`Updated validation comment: id=${existing.id} url=${res.data.html_url}`);
    return { action: 'updated', comment_id: existing.id, url: res.data.html_url };
  }

  const res = await github.rest.issues.createComment({
    owner,
    repo,
    issue_number,
    body,
  });
  console.log(`Created validation comment: id=${res.data.id} url=${res.data.html_url}`);
  return { action: 'created', comment_id: res.data.id, url: res.data.html_url };
}

module.exports = { run };
