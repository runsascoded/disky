from disk_tree.env import get, name


def test_new_name_wins_old_name_falls_back_then_default():
    assert [
        get('ROOT', 'd', {'DISKY_ROOT': 'new', 'DISK_TREE_ROOT': 'old'}),
        get('ROOT', 'd', {'DISK_TREE_ROOT': 'old'}),
        get('ROOT', 'd', {}),
        get('ROOT', env={}),
        get('ROOT', 'd', {'DISKY_ROOT': ''}),
    ] == ['new', 'old', 'd', None, '']


def test_a_child_is_handed_the_new_name():
    assert name('SCAN_DIRS') == 'DISKY_SCAN_DIRS'
