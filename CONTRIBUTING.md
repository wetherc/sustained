# Contributing to Sustained

[DEVELOPERS.md](DEVELOPERS.md) describes how the query builder, the compilers, and the migration tooling work inside. Read it before you change code in those areas.

## License of contributions

Sustained is licensed under the MIT License in [LICENSE](LICENSE). When you open a pull request, you license your contribution under the MIT License, on the same terms as the rest of the project. You keep the copyright in your contribution, and LICENSE names the holders as "Christopher Wetherill and contributors".

## Contributor License Agreement

Before your first pull request is merged, you sign the [Sustained Individual Contributor License Agreement](CLA.md) once. The agreement covers every contribution that you make after that.

The agreement does not transfer your copyright. It gives the maintainer a license to your contribution, with a patent license, and it allows the maintainer to license your contribution under the project's current license or under any license that the Open Source Initiative approves. The agreement does not allow a proprietary license.

If your employer or a client can own the code that you write, get their approval of the agreement before you sign.

## Sign the Contributor License Agreement

You sign the agreement with a commit that you sign cryptographically, in a pull request of its own, before your first contribution. The signed commit stays in the project's git history, so it records exactly which version of the agreement you accepted and which key signed it. No other way to sign is accepted.

The signing takes about 15 minutes the first time. The steps are:

1. [Check what you need](#check-what-you-need).
2. Set up a signing key, with [SSH](#set-up-an-ssh-signing-key) or with [GPG](#set-up-a-gpg-signing-key).
3. [Link your commit email to GitHub](#link-your-commit-email-to-github).
4. [Make the signing commit](#make-the-signing-commit).
5. [Open the signing pull request](#open-the-signing-pull-request).

### Check what you need

- A GitHub account.
- Git 2.34 or later for an SSH key, or any Git with GnuPG for a GPG key. Run `git --version` to check.
- Python 3, which the step that edits `.clabot` uses.
- Your employer's or client's approval of the agreement, if they can own the code that you write.

The signature is a permanent, public record. Your full legal name, your GitHub account name, and your commit email address stay in the project's git history and in every copy of it, as clause (d) of the Developer Certificate of Origin also states. To keep your personal email address private, use your GitHub no-reply address (`<id>+<login>@users.noreply.github.com`, shown at https://github.com/settings/emails) as your commit email.

### Set up an SSH signing key

Use SSH if you already use an SSH key with GitHub, or if you do not want to install GnuPG. You can use the key that you use to push. Do the steps in [Set up a GPG signing key](#set-up-a-gpg-signing-key) instead if you prefer GPG.

1. If you have no SSH key, create one:

   ```sh
   ssh-keygen -t ed25519 -C "you@example.com"
   ```

   Accept the default file, `~/.ssh/id_ed25519`, and set a passphrase.

2. Add the public key to GitHub as a **signing key**. A key that you added only as an authentication key does not verify commits, even when it is the same key. Either use the GitHub CLI:

   ```sh
   gh ssh-key add ~/.ssh/id_ed25519.pub --type signing --title "Commit signing"
   ```

   Or go to https://github.com/settings/ssh/new, set **Key type** to **Signing Key**, and paste the contents of `~/.ssh/id_ed25519.pub`.

3. Tell Git to sign with the key:

   ```sh
   git config --global gpg.format ssh
   git config --global user.signingkey ~/.ssh/id_ed25519.pub
   ```

4. Let Git check SSH signatures on your machine. Without this file, `git log --show-signature` cannot verify your commit, although GitHub still can:

   ```sh
   mkdir -p ~/.config/git
   echo "$(git config --get user.email) namespaces=\"git\" $(cat ~/.ssh/id_ed25519.pub)" >> ~/.config/git/allowed_signers
   git config --global gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers
   ```

   Set your commit email (see [Link your commit email to GitHub](#link-your-commit-email-to-github)) before you run these commands, because the first line reads it.

### Set up a GPG signing key

1. Install GnuPG. On macOS, run `brew install gnupg pinentry-mac`. On Debian or Ubuntu, run `sudo apt install gnupg`. On Windows, Git for Windows includes GnuPG.

2. Create a key. Use the email address that you will commit with, and set a passphrase:

   ```sh
   gpg --full-generate-key
   ```

   Choose "ECC (sign only)" with Curve 25519, or RSA with 4096 bits.

3. Find the key ID, which is the part after the slash on the `sec` line:

   ```sh
   gpg --list-secret-keys --keyid-format=long
   ```

   ```
   sec   ed25519/3AA5C34371567BD2 2026-10-14 [SC]
   ```

4. Add the public key to GitHub. Either use the GitHub CLI:

   ```sh
   gpg --armor --export 3AA5C34371567BD2 > key.asc
   gh gpg-key add key.asc
   ```

   Or go to https://github.com/settings/gpg/new and paste the output of `gpg --armor --export 3AA5C34371567BD2`.

5. Tell Git to sign with the key:

   ```sh
   git config --global gpg.format openpgp
   git config --global user.signingkey 3AA5C34371567BD2
   ```

6. Let GnuPG ask for your passphrase. On macOS:

   ```sh
   echo "pinentry-program $(brew --prefix)/bin/pinentry-mac" >> ~/.gnupg/gpg-agent.conf
   gpgconf --kill gpg-agent
   ```

   On Linux, add `export GPG_TTY=$(tty)` to your shell profile and open a new terminal.

7. Test the key. The command prints a signed message when signing works:

   ```sh
   echo test | gpg --clearsign
   ```

The email address in the key must match your commit email, and that email must be verified on your GitHub account. If they differ, GitHub shows the commit as "Unverified".

### Link your commit email to GitHub

cla-bot finds your GitHub account through the email address on your commits, and GitHub verifies a signature only when that address is verified on your account.

1. Check your commit email:

   ```sh
   git config --get user.email
   ```

2. Make sure that the address is listed and verified at https://github.com/settings/emails. To use the no-reply address instead, set it:

   ```sh
   git config --global user.email "<id>+<login>@users.noreply.github.com"
   ```

3. Set your name for commits, if you have not:

   ```sh
   git config --global user.name "Your Name"
   ```

### Make the signing commit

1. Fork https://github.com/wetherc/sustained on GitHub, then clone your fork and make a branch from the current `main`:

   ```sh
   git clone https://github.com/<login>/sustained.git
   cd sustained
   git remote add upstream https://github.com/wetherc/sustained.git
   git fetch upstream
   git switch -c sign-cla upstream/main
   ```

2. Set your full legal name and your GitHub account name, and build the statement. The statement must be this text exactly, with only the name and the account filled in:

   ```sh
   NAME="Your Full Legal Name"
   LOGIN="your-github-login"
   STATEMENT="I, $NAME, GitHub account $LOGIN, have read the Sustained Individual Contributor License Agreement, version 1.0, at https://github.com/wetherc/sustained/blob/ef2d45757b3891b99b23a04e55e5ee47a4352071/CLA.md, and I agree to its terms for my contributions to Sustained."
   ```

   Read the agreement at the link in the statement before you continue. The link points to one fixed version of the agreement. If the agreement changes, this page gives a new statement, and your signature continues to cover the version that you signed.

3. Write the statement to your signature file:

   ```sh
   mkdir -p cla/signatures
   printf '%s\n' "$STATEMENT" > "cla/signatures/$LOGIN.txt"
   ```

4. Add your account to the `contributors` list in `.clabot`. This command keeps the file's format:

   ```sh
   python3 -c 'import json, sys; p = ".clabot"; d = json.load(open(p)); d["contributors"] = sorted(set(d["contributors"]) | {sys.argv[1]}, key=str.lower); open(p, "w").write(json.dumps(d, indent=2) + "\n")' "$LOGIN"
   ```

5. Commit the two files with a signature (`-S`) and a sign-off (`-s`), with the statement in the commit message:

   ```sh
   git add "cla/signatures/$LOGIN.txt" .clabot
   git commit -S -s -m "Sign the Sustained Contributor License Agreement" -m "$STATEMENT"
   ```

6. Check the commit:

   ```sh
   git log --show-signature -1
   git show --stat HEAD
   ```

   The first command prints `Good signature` (SSH) or `Good signature from` (GPG), the statement, and a `Signed-off-by` line. The second lists only `.clabot` and your signature file.

### Open the signing pull request

1. Push the branch to your fork:

   ```sh
   git push -u origin sign-cla
   ```

2. Open a pull request against `main` of `wetherc/sustained` that contains only the signing commit. With the GitHub CLI:

   ```sh
   gh pr create --repo wetherc/sustained --base main --title "Sign the CLA: $LOGIN" --body "$STATEMENT"
   ```

3. On the pull request's **Commits** tab, check that your commit shows **Verified**. If it shows **Unverified**, see [Troubleshooting](#troubleshooting) before the maintainer reviews it.

cla-bot reports a failure on the signing pull request, because your account is not on the list until the pull request is merged. Leave it as it is.

After the maintainer merges the signing pull request, update your contribution branches and open your contributions as usual:

```sh
git fetch upstream
git rebase upstream/main
```

If you opened a contribution before your signature was merged, comment `@cla-bot check` on it. If more than one person made commits in a pull request, each person signs with their own signing pull request first.

### Troubleshooting

**GitHub shows "Unverified".** Click the badge to see the reason. The usual causes:

- The commit email is not verified on your GitHub account. Fix it at https://github.com/settings/emails.
- An SSH key was added as an authentication key only. Add it again as a signing key.
- A GPG key's email does not match the commit email, or the key has expired. Run `gpg --list-keys` to check.

After the fix, sign the commit again and update the pull request:

```sh
git commit --amend --no-edit -S -s
git push --force-with-lease
```

**`error: gpg failed to sign the data`.** GnuPG cannot ask for your passphrase. Do step 6 of [Set up a GPG signing key](#set-up-a-gpg-signing-key), then run `echo test | gpg --clearsign` again.

**`gpg.ssh.allowedSignersFile needs to be configured`.** Git signed the commit but cannot check SSH signatures on your machine. Do step 4 of [Set up an SSH signing key](#set-up-an-ssh-signing-key). GitHub verifies the commit without that file.

**cla-bot says it could not parse the GitHub identity.** The commit email is not linked to a GitHub account. See [Link your commit email to GitHub](#link-your-commit-email-to-github), then amend and push as above.

**The maintainer asks you to rebase.** The maintainer merges a signing pull request only as a fast-forward of `main`. Rebase and sign again:

```sh
git fetch upstream
git rebase -S upstream/main
git push --force-with-lease
```

If `.clabot` has a conflict because another person signed first, keep both names, then run `git add .clabot` and `git rebase --continue`.

**The agreement changed after you signed.** Your signature covers the version that you signed. To accept a new version, follow [Make the signing commit](#make-the-signing-commit) again with the new statement from this page. It replaces the text of your signature file, and git history keeps the earlier one.

**You cannot set up commit signing.** Open an issue that describes the problem. No other way to sign is accepted.

### Signature record

The maintainer checks each signing pull request before merging it:

- The pull request has one commit, and the commit changes only `cla/signatures/<login>.txt` and the `contributors` list in `.clabot`.
- GitHub shows the commit as "Verified", with a key that belongs to the account that is named in the file.
- The statement in the file and in the commit message matches the statement in [Make the signing commit](#make-the-signing-commit), apart from the name and the account.
- The link names the current version of the agreement.

The maintainer merges the signing pull request as a fast-forward from a local clone (`git merge --ff-only`, then `git push`). A squash merge or a rebase merge on GitHub makes a new commit and drops your signature, so the maintainer does not use them for a signing pull request. After the merge, the maintainer comments `@cla-bot check` on your open pull requests, and cla-bot adds the `cla-signed` label. You sign once, and later pull requests pass the check.

## Work you did not write

Submit only work that you wrote, unless the other work is under a license that is compatible with the MIT License. In the pull request description, name each file or passage that you did not write, its source, and its license. Do not submit work whose license you do not know.

## Developer Certificate of Origin

Every commit in a pull request needs a sign-off line. With the sign-off, you certify the Developer Certificate of Origin (DCO) 1.1, which is reproduced at the end of this page. The certificate states that you wrote the change, or that you otherwise have the right to submit it under the MIT License.

If your employer or a client can own the code that you write, get their permission before you sign off. A pull request is not merged until every commit in it has a sign-off and you have signed the Contributor License Agreement.

## Sign off your commits

Add `-s` to `git commit`:

```sh
git commit -s -m "Describe the change"
```

Git appends a line with the name and email address from your git configuration:

```
Signed-off-by: Jane Developer <jane@example.com>
```

Use your real name in the sign-off.

## Add a missing sign-off

To sign off the last commit:

```sh
git commit --amend -s --no-edit
```

To sign off every commit on your branch:

```sh
git rebase --signoff main
```

Then update the pull request with `git push --force-with-lease`.

## Certificate text

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```
