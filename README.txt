For instructions on the usage, see http://github.com/dtrott/git-p4

This version of git-p4 stores info in git notes instead of rewriting history. This means you'll
need git >= 1.7.1 for it to work.

The following options can be set in the config file:
	git-p4.user
	git-p4.password
	git-p4.port
	git-p4.host
	git-p4.client
	git-p4.detectRename (only affects submitting from git to p4)
	git-p4.detectCopy (only affects submitting from git to p4)
	git-p4.allowSubmit (comma-separated list of branch names that are allowed to submit to p4)
	git-p4.syncFromOrigin
	git-p4.useclientspec
	git-p4.importIntoRemotes

