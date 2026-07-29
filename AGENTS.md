## No Artificial Workflow or Excessive Task Decomposition

This is a solo-maintained project.

Do not create or simulate:

- Owners, Reviewers, Verifiers, Approvers, or Delivery Managers;
- role-based handoffs or staged ownership transfers;
- SHA freeze tasks or baseline-freezing phases;
- approval gates, sign-offs, or independent verification phases;
- separate process tasks that produce no code, tests, or required documentation.

Testing, linting, type checking, regression checks, and secret scans are technical verification steps, not organizational approval stages.

For normal implementation work, use one continuous workflow:

1. inspect the relevant code;
2. implement the requested change;
3. update directly affected tests or required documentation;
4. run focused verification;
5. report the result and continue.

Do not decompose a normal request into more than 3–5 concrete execution steps.

Each step must directly contribute code, tests, required documentation, or meaningful verification. Do not create separate steps for:

- assigning ownership;
- freezing baselines;
- transferring work between simulated roles;
- approving completed work;
- re-running checks already covered by final verification;
- producing process artifacts.

Merge implementation, directly related documentation, and focused verification whenever practical.

Do not wait for approval after presenting a plan. Continue immediately unless there is an actual technical blocker, high-risk ambiguity, or an operation that explicitly requires confirmation.