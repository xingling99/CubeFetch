# -*- coding: utf-8 -*-
"""CubeFetch 文件完整性清单（自动生成，请勿手动修改）。

由 ``build/gen_integrity.py`` 生成：
- ``FILES``：相对仓库根的源码文件 sha256（源码模式校验）；
- ``CODE``：各模块规范化 code object 的 sha256（PyInstaller 冻结模式校验）。

修改源码后必须重新运行 ``python -X utf8 build/gen_integrity.py``。
"""

BUILD_TIME = "2026-10-23T10:36:58Z"

FILES = {
    'cubefetch/__init__.py': '12999de39a79ea22b23407923007056828527c092c1db77ae1d85123897db69d',
    'cubefetch/config.py': 'cf4d64895c879193e94490732831154f539513d0394325b80058ec8b7676783b',
    'cubefetch/core/__init__.py': 'caf68e983d813e2c676142160e3afd7d54b3670fcad6ecd7347aaf918d3b9b3e',
    'cubefetch/core/account_store.py': '0972eb34c1cfd0b2e1eac842305988afdbd82c2b417b5aba867a74cd271ee59f',
    'cubefetch/core/auth.py': '1e15ac62058e29ce4643e24a2015204bc1163364aefcf2b13f57a9da1d6a697b',
    'cubefetch/core/detect.py': 'd15d9c4cba55008c786a3ff690e54f717c772f6a0afed6e818852c7de2775c5f',
    'cubefetch/core/errors.py': 'a1075be9f3caeeda4fa0a6a64482e7c5cb16c96e56e065119fc52e89024d6888',
    'cubefetch/core/http.py': '46ba92ca75ca9ecc61a804e34b84ce3579f22ee44f0753a065dcea7bee8f6ce2',
    'cubefetch/core/install.py': 'cdb963d3e9055fced27719e499b9275c5ffed6e7ff7cecdba38afacd1a9fc253',
    'cubefetch/core/installer_processors.py': 'e4c521a5cb21962cebf3cc7943ae890250aacf83611468e76692641a33bfef78',
    'cubefetch/core/integrity.py': 'c991088e7e88f3b5469607dba1e58644014807c61b618bbffd32296a1bf50f63',
    'cubefetch/core/isolation.py': 'f47dfd726939c4b2bfeaef35a2c22b809bde1d74a6090be0ccfd063144d7b99d',
    'cubefetch/core/java_runtime.py': 'b9e86e795443b733048383602317ac23280a63c951b95659ff40b4efd7183fc6',
    'cubefetch/core/loader.py': '2ec959f6857582ff596db101f3f11b5d5659bb58ca9b5a0b72dcbc5db4a620b4',
    'cubefetch/core/manifest.py': '18883d095ecf067c556e37fea0dae53498b99ed5ce3d980179224ef5b9293c0d',
    'cubefetch/core/security.py': '4a912bec8d5f33719d1813d7d65b81eb810a4e1f1ace1885ecd96c74e6569735',
    'cubefetch/core/util.py': '6e1aab29eab6c38915a10b7a569db469ed28bb2ded18f0572ad9756a6aa1beb5',
    'cubefetch/core/version_rename.py': '77fe9e44a2135e6ff00f533932cd8ee2e868e62acc1bdab82d7c6203d15416bd',
    'cubefetch/services.py': '08038d95b02513f49e08a4d5dfeaeb3d2a3058129b26c74e585df419aee8e21e',
    'cubefetch/ui/__init__.py': '7d6fef980d12cac29ccf0ec6ec6f6fe11102a5b1c4f0843120807f2078815296',
    'cubefetch/ui/_selftest.py': 'f72b62130108ec06462caab9250fe512beea5f1e95c13181ea64a29699e36794',
    'cubefetch/ui/app_window.py': 'ddc0594b3b90562d44d6cbba12e376ed0046c1a3489f6537bbd1df7b2e15386e',
    'cubefetch/ui/dialogs.py': '5bb5e77641c930e888d3da24922dfee3c56a2130942bdf430b941f50f2b9e82c',
    'cubefetch/ui/dpi.py': '921f34f0787c868458892e92e8907a71b8bcd1a2f38c2ffedad1e02076eacffc',
    'cubefetch/ui/pages/__init__.py': '6a329bd3b545a72503a68b3d285240d7e507f9feb7a6a6ece900daba57892ad5',
    'cubefetch/ui/pages/base.py': '920165023ca9234872481cc7579ddf27feb9034fa13862b9b0642007bed5fa56',
    'cubefetch/ui/pages/done.py': '99af2eae95b20eff84f29f142c34acc3ea12ae04030081082375d1d71bf4bf43',
    'cubefetch/ui/pages/download.py': 'ef2586e1262c10687aa8cfcaa7213d0263572b864d8fe9cbda60b1e6e5c86a36',
    'cubefetch/ui/pages/login.py': '232ddf528df42af1c3b2d6476e409587c3b9dda252da15a413010a3104f20fd6',
    'cubefetch/ui/pages/progress.py': '9e8f5720061c2d2a866efa93bbd29fa769604e2404c0ee791de36571bcdac555',
    'cubefetch/ui/pages/settings.py': '1a4bec8887e1242d3019ba6dfc2c988f7c72355b77bafd3ae0645f45bdd1174e',
    'cubefetch/ui/sound.py': 'b748f97d4512d7e18bfe71b2fdeb9a00d015bead4dae6ec3c03595a23254ee47',
    'cubefetch/ui/theme.py': '964c551461bd5fd25003d749ab4dbcdf5276c353219f3c2437a84c09ff80f66c',
    'cubefetch/ui/widgets.py': '9c4c5782138824883c17a3f0023b8054f7eb14308f6dad44c7bbe8a0b29b7ccc',
}

CODE = {
    'cubefetch': '9964847bbc6d364c534c931bed142240d92d725ebd64ffe29acceba2306a27d0',
    'cubefetch.config': 'e02b30b1bf2233aabc3f02e3dea969ead18fc7fb5d57329b589b0cd24e9cb64c',
    'cubefetch.core': '0218775815b6910ebf0558db8a91aaadfefaf7c6dbab0cdae34c9b2ece64cce8',
    'cubefetch.core.account_store': '5540e6316360c9d5e54945fb6935a3446fc6c08881d78c253e41171e66db0bfa',
    'cubefetch.core.auth': 'e9ec8739b7e58c54b2cd4b7fc9b8665987c56f8eea6933857061d656d18f5a72',
    'cubefetch.core.detect': 'c1f38e34d5c1e76dd2c976f18331e9ae5df00130cfb30b6ddeba538e37d444b8',
    'cubefetch.core.errors': '1387c2d117fa052dff4b870f5e884402965b89749ac1fb1d0ef29294167de9ef',
    'cubefetch.core.http': '4fc953349bbdbe9ab2ad9edea5935f768e1cb707483c88963fa9c77df506cae3',
    'cubefetch.core.install': 'b7730dfde9c45a6369a7ae2ab5d6a369f0ae6c893b844721c0ed69473db49db5',
    'cubefetch.core.installer_processors': '6c0be6cd7dbaf3c82f2cd3b9d87ebae4a69bbd3f00e7f037436ba34c13d9c99c',
    'cubefetch.core.integrity': 'c209dbddd3e65dc7542533181b5f79ac904f8047c1b7d0c927593935a0dfda59',
    'cubefetch.core.isolation': 'f9fb036ce82d58085dd8a4c1b840abfc2acf49550252dfa2b1bdc055c5d4836c',
    'cubefetch.core.java_runtime': 'd9e141251e46d077335c8f97d1eb89e468b11fd13bd0ab7e2fb9ad49e9ed3e29',
    'cubefetch.core.loader': '9637f72cdd09e3db52e726da292ef0b7f0875bba5625b8100372714e0ade9bdc',
    'cubefetch.core.manifest': '98a2a61304ae0b0c07a605934b76efffe04f70b511bbc2493182b430290f43eb',
    'cubefetch.core.security': '61550ab2ee39b88755b421d942dfc4cfdf6cb4b2267226b836e8c177862823e7',
    'cubefetch.core.util': '88aa676b6e9c2a669f8917adc0241db1dcc74e963ed2da2f31fa5c1eb687fbb0',
    'cubefetch.core.version_rename': 'c4b5a866f3f522d546e468f03bb1730ce4c8d9899f7b12f517f49e34ea95b6d7',
    'cubefetch.services': 'abd2873fac6048d95ab4362801eac93656a4bd3d52c837825983615f7e0867a9',
    'cubefetch.ui': '945cc9b25e5863a82f4e4de9038efbc41acc12500e90b3d65eda3426f187a257',
    'cubefetch.ui._selftest': '84e7594f90d5d8a2e79655d7d94b824ad26aec70be267124eb05e6d05c429f87',
    'cubefetch.ui.app_window': '53f40067aef9fa71f023c2a670cd4126f2c2aa8a3fa35e52751bce43e73d7808',
    'cubefetch.ui.dialogs': 'f08d18409b5426e90e30d62a7b37e14b067128bc28f98a054745aa0f35cad2d2',
    'cubefetch.ui.dpi': 'd07c8028a25d3cb7d078ed9029ad0975fb28b76b3e8d24a54226b71e18592031',
    'cubefetch.ui.pages': 'a6dedbd987b1eb6b55dc76eb011d95278074b368af265a3d1135069a5a3e84e8',
    'cubefetch.ui.pages.base': '9ed24b5c77c19ba68a848ed5d9afa6c128d6b148d414262b1ea36c386d79dea4',
    'cubefetch.ui.pages.done': '91626c7e6f6c568e2ed02607c82d444528098f82947eaaa7aeb380175f7365c9',
    'cubefetch.ui.pages.download': '5b707679212bc8418636a71af7b1a15e1651dfe2653f6a4afa6b0f685c57b09a',
    'cubefetch.ui.pages.login': 'd03f90951096cff4f7314342457ea173c4db92e431f2af2f2080f899b1f63f7c',
    'cubefetch.ui.pages.progress': 'bfa22fdc155d5553c3fb584afe36f2c8972e506be55d21ac43333c1f5eaf5787',
    'cubefetch.ui.pages.settings': '1c71b394211c7ba748e010f4effc5b58792c05b5cff7dbe5207e3847ff87f1cc',
    'cubefetch.ui.sound': '30ad406853945c0ebdc7bf64ba882a2d1e2e362844580821341712552195a844',
    'cubefetch.ui.theme': '1bb8f7262206c4dca04f3fed0775b02f7ead0215a8769abde080cbec0ccf1999',
    'cubefetch.ui.widgets': '1689b740ec74cb207e7ff0226cca02a216c23834d1af0fb442f8c8db992d3719',
}
