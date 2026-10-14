# Contributing to Sustained

[DEVELOPERS.md](DEVELOPERS.md) describes how the query builder, the compilers, and the migration tooling work inside. Read it before you change code in those areas.

## License of contributions

Sustained is licensed under the MIT License in [LICENSE](LICENSE). When you open a pull request, you license your contribution under the MIT License, on the same terms as the rest of the project. You keep the copyright in your contribution, and LICENSE names the holders as "Christopher Wetherill and contributors".

## Contributor License Agreement

Before your first pull request is merged, you sign the [Sustained Individual Contributor License Agreement](CLA.md) once. The agreement covers every contribution that you make after that.

The agreement does not transfer your copyright. It gives the maintainer a license to your contribution, with a patent license, and it allows the maintainer to license your contribution under the project's current license or under any license that the Open Source Initiative approves. The agreement does not allow a proprietary license.

If your employer or a client can own the code that you write, get their approval of the agreement before you sign.

## Work you did not write

Submit only work that you wrote, unless the other work is under a license that is compatible with the MIT License. In the pull request description, name each file or passage that you did not write, its source, and its license. Do not submit work whose license you do not know.

## Developer Certificate of Origin

Every commit in a pull request needs a sign-off line. With the sign-off, you certify the Developer Certificate of Origin (DCO) 1.1, which is reproduced at the end of this page. The certificate states that you wrote the change, or that you otherwise have the right to submit it under the MIT License.

If your employer or a client can own the code that you write, get their permission before you sign off. A pull request is not merged until every commit in it has a sign-off and you have signed the Contributor License Agreement.

## Sign your commits

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
