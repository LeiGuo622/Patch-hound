from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.commit_info import _format_useful_diff  # noqa: E402


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


class CommitDiffParserTest(unittest.TestCase):
    def test_new_file_records_dev_null_before_path(self) -> None:
        raw_diff = _dedent(
            """
            diff --git a/drivers/pinctrl/airoha/Kconfig b/drivers/pinctrl/airoha/Kconfig
            new file mode 100644
            index 0000000000000..03adaeae8fc3a
            --- /dev/null
            +++ b/drivers/pinctrl/airoha/Kconfig
            @@ -0,0 +1,2 @@
            +config PINCTRL_AIROHA
            +	tristate "Airoha EN7581 pin control"
            """
        )

        self.assertEqual(
            _format_useful_diff(raw_diff),
            _dedent(
                """
                File: drivers/pinctrl/airoha/Kconfig
                Before: /dev/null
                After: drivers/pinctrl/airoha/Kconfig
                new file mode 100644
                @@ -0,0 +1,2 @@
                +config PINCTRL_AIROHA
                +	tristate "Airoha EN7581 pin control"
                """
            ),
        )

    def test_deleted_file_records_dev_null_after_path(self) -> None:
        raw_diff = _dedent(
            """
            diff --git a/fs/erofs/fscache.c b/fs/erofs/fscache.c
            deleted file mode 100644
            index 4d5770831911..000000000000
            --- a/fs/erofs/fscache.c
            +++ /dev/null
            @@ -1,2 +0,0 @@
            -// SPDX-License-Identifier: GPL-2.0-or-later
            -#include <linux/fscache.h>
            """
        )

        self.assertEqual(
            _format_useful_diff(raw_diff),
            _dedent(
                """
                File: fs/erofs/fscache.c
                Before: fs/erofs/fscache.c
                After: /dev/null
                deleted file mode 100644
                @@ -1,2 +0,0 @@
                -// SPDX-License-Identifier: GPL-2.0-or-later
                -#include <linux/fscache.h>
                """
            ),
        )

    def test_rename_with_edits_records_both_paths_and_hunks(self) -> None:
        raw_diff = _dedent(
            """
            diff --git a/fs/smb/client/compress/lz77.c b/fs/smb/common/compress/lz77.c
            similarity index 75%
            rename from fs/smb/client/compress/lz77.c
            rename to fs/smb/common/compress/lz77.c
            index 0d849e206b87..e18694cba584 100644
            --- a/fs/smb/client/compress/lz77.c
            +++ b/fs/smb/common/compress/lz77.c
            @@ -1,2 +1,2 @@
            -int lz77_compress(void);
            +int smb_lz77_compress(void);
            """
        )

        self.assertEqual(
            _format_useful_diff(raw_diff),
            _dedent(
                """
                File: fs/smb/common/compress/lz77.c
                Before: fs/smb/client/compress/lz77.c
                After: fs/smb/common/compress/lz77.c
                similarity index 75%
                rename from fs/smb/client/compress/lz77.c
                rename to fs/smb/common/compress/lz77.c
                @@ -1,2 +1,2 @@
                -int lz77_compress(void);
                +int smb_lz77_compress(void);
                """
            ),
        )

    def test_copy_and_mode_change_records_source_and_destination(self) -> None:
        raw_diff = _dedent(
            """
            diff --git a/tools/perf/trace/beauty/fsmount.sh b/tools/perf/trace/beauty/fsmount_attr.sh
            old mode 100755
            new mode 100644
            similarity index 100%
            copy from tools/perf/trace/beauty/fsmount.sh
            copy to tools/perf/trace/beauty/fsmount_attr.sh
            """
        )

        self.assertEqual(
            _format_useful_diff(raw_diff),
            _dedent(
                """
                File: tools/perf/trace/beauty/fsmount_attr.sh
                Before: tools/perf/trace/beauty/fsmount.sh
                After: tools/perf/trace/beauty/fsmount_attr.sh
                old mode 100755
                new mode 100644
                similarity index 100%
                copy from tools/perf/trace/beauty/fsmount.sh
                copy to tools/perf/trace/beauty/fsmount_attr.sh
                """
            ),
        )

    def test_binary_file_records_dev_null_side(self) -> None:
        raw_diff = _dedent(
            """
            diff --git a/Documentation/media/uapi/v4l/bayer.png b/Documentation/media/uapi/v4l/bayer.png
            deleted file mode 100644
            index 9b15fb22e8173..0000000000000
            Binary files a/Documentation/media/uapi/v4l/bayer.png and /dev/null differ
            diff --git a/firmware/blob.bin b/firmware/blob.bin
            new file mode 100644
            index 0000000000000..1111111111111
            Binary files /dev/null and b/firmware/blob.bin differ
            """
        )

        self.assertEqual(
            _format_useful_diff(raw_diff),
            _dedent(
                """
                File: Documentation/media/uapi/v4l/bayer.png
                Before: Documentation/media/uapi/v4l/bayer.png
                After: /dev/null
                deleted file mode 100644
                Binary files a/Documentation/media/uapi/v4l/bayer.png and /dev/null differ

                File: firmware/blob.bin
                Before: /dev/null
                After: firmware/blob.bin
                new file mode 100644
                Binary files /dev/null and b/firmware/blob.bin differ
                """
            ),
        )

    def test_combined_diff_records_file_path(self) -> None:
        raw_diff = _dedent(
            """
            diff --combined kernel/sched/core.c
            index 111111111111,222222222222..333333333333
            --- a/kernel/sched/core.c
            +++ b/kernel/sched/core.c
            @@@ -1,2 -1,2 +1,2 @@@
            - old
            + new
            """
        )

        self.assertEqual(
            _format_useful_diff(raw_diff),
            _dedent(
                """
                File: kernel/sched/core.c
                Before: kernel/sched/core.c
                After: kernel/sched/core.c
                @@@ -1,2 -1,2 +1,2 @@@
                - old
                + new
                """
            ),
        )

    def test_paths_with_spaces_are_not_truncated(self) -> None:
        raw_diff = _dedent(
            """
            diff --git "a/old name.txt" "b/new name.txt"
            similarity index 90%
            rename from old name.txt
            rename to new name.txt
            --- a/old name.txt
            +++ b/new name.txt
            @@ -1 +1 @@
            -old
            +new
            """
        )

        self.assertEqual(
            _format_useful_diff(raw_diff),
            _dedent(
                """
                File: new name.txt
                Before: old name.txt
                After: new name.txt
                similarity index 90%
                rename from old name.txt
                rename to new name.txt
                @@ -1 +1 @@
                -old
                +new
                """
            ),
        )


if __name__ == "__main__":
    unittest.main()
