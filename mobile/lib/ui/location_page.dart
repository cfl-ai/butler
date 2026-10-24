/// 定位 / 推荐 / 导航页（U71）。
///
/// 这一页最重要的一件事是**不装样子**：
///
/// * 本仓库没有任何地图服务 key，所以**不画底图**，只显示坐标与「未配置」说明；
/// * 推荐用的是示例地点库时，必须挂一个「示例数据」角标 ——
///   把示例当真实推荐展示，比没有推荐更糟；
/// * 导航链接由服务端按各家 URI scheme 生成（已转好 GCJ-02 / BD-09），
///   端侧只负责用系统打开；**起点默认不传**，让导航 App 自己取「我的位置」。
library;

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:aibutler_core/aibutler_core.dart';

class LocationPage extends StatefulWidget {
  const LocationPage({
    super.key,
    required this.onLocate,
    required this.onRecommend,
    required this.onNavigation,
    this.initialPrefs = const [],
  });

  /// 取当前坐标。返回 null 表示没取到（未授权 / 无定位服务）。
  final Future<({double latitude, double longitude})?> Function() onLocate;
  final Future<RecommendResult> Function(
      double? lat, double? lon, List<String> prefs) onRecommend;
  final Future<List<NavLink>> Function(RecommendItem item) onNavigation;

  final List<String> initialPrefs;

  @override
  State<LocationPage> createState() => _LocationPageState();
}

class _LocationPageState extends State<LocationPage> {
  RecommendResult? _result;
  ({double latitude, double longitude})? _position;
  late List<String> _prefs = [...widget.initialPrefs];
  bool _busy = false;
  String? _error;

  static const _prefOptions = <String>[
    '安静', '咖啡', '书店', '看展', '散步', '户外', '吃饭', '夜宵', '学习', '宠物',
  ];

  @override
  void initState() {
    super.initState();
    _refresh(withLocation: true);
  }

  Future<void> _refresh({required bool withLocation}) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      ({double latitude, double longitude})? position;
      if (withLocation) {
        position = await widget.onLocate();
      } else {
        position = _position;
      }
      final result = await widget.onRecommend(
          withLocation ? position?.latitude : null,
          withLocation ? position?.longitude : null,
          _prefs);
      if (!mounted) return;
      setState(() {
        _position = position;
        _result = result;
        if (withLocation && position == null) {
          _error = '没有取到定位（可能未授权或系统定位已关闭）。'
              '下面的推荐已降级为「只按喜好」。';
        }
      });
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _openNavigation(RecommendItem item) async {
    if (!item.hasCoordinate) {
      setState(() => _error = '这条没有坐标，无法导航');
      return;
    }
    setState(() => _error = null);
    try {
      final links = await widget.onNavigation(item);
      if (!mounted) return;
      await showModalBottomSheet<void>(
        context: context,
        showDragHandle: true,
        builder: (context) => SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ListTile(
                title: Text('导航到 ${item.name}'),
                subtitle: const Text('起点由导航 App 自己取「我的位置」，我方坐标不外传'),
              ),
              const Divider(height: 1),
              ...links.map((link) => ListTile(
                    leading: Icon(_iconFor(link.app)),
                    title: Text(link.label),
                    subtitle: Text(
                      '坐标系 ${link.coordinateSystem}'
                      '${link.note.isEmpty ? "" : " · ${link.note}"}',
                    ),
                    onTap: () async {
                      final navigator = Navigator.of(context);
                      final messenger = ScaffoldMessenger.of(context);
                      final uri = Uri.tryParse(link.url);
                      navigator.pop();
                      // `launchUrl` 在「没装这个 App」时**可能抛
                      // PlatformException 而不是返回 false**（各家 ROM 不一致），
                      // 所以两种失败都要接住，否则用户点一下只会闪退到桌面。
                      var opened = false;
                      try {
                        opened = uri != null &&
                            await launchUrl(uri,
                                mode: LaunchMode.externalApplication);
                      } catch (_) {
                        opened = false;
                      }
                      if (!opened) {
                        messenger.showSnackBar(
                          SnackBar(content: Text('没能打开 ${link.label}，'
                              '可能未安装。试试「网页查看位置」。')),
                        );
                      }
                    },
                  )),
            ],
          ),
        ),
      );
    } catch (error) {
      if (mounted) setState(() => _error = error.toString());
    }
  }

  static IconData _iconFor(String app) => switch (app) {
        'amap' => Icons.map,
        'baidu' => Icons.near_me,
        'tencent' => Icons.explore,
        _ => Icons.public,
      };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final result = _result;

    return Scaffold(
      appBar: AppBar(
        title: const Text('附近与推荐'),
        actions: [
          IconButton(
            tooltip: '重新定位',
            onPressed: _busy ? null : () => _refresh(withLocation: true),
            icon: const Icon(Icons.my_location),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () => _refresh(withLocation: true),
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Icon(Icons.place_outlined, color: theme.colorScheme.primary),
                        const SizedBox(width: 8),
                        Text('当前位置', style: theme.textTheme.titleMedium),
                        const Spacer(),
                        if (_busy)
                          const SizedBox(
                              height: 16,
                              width: 16,
                              child: CircularProgressIndicator(strokeWidth: 2)),
                      ],
                    ),
                    const SizedBox(height: 8),
                    if (result?.locatedAtGrid != null)
                      Text(
                        '${result!.locatedAtGrid!.latitude.toStringAsFixed(3)}, '
                        '${result.locatedAtGrid!.longitude.toStringAsFixed(3)}',
                        style: theme.textTheme.titleLarge,
                      )
                    else
                      Text('未取得定位', style: theme.textTheme.titleLarge),
                    const SizedBox(height: 4),
                    Text(
                      '上报前已粗化到约百米网格，精确坐标不出设备。',
                      style: theme.textTheme.bodySmall,
                    ),
                    if (result != null && !result.mapAvailable) ...[
                      const SizedBox(height: 8),
                      Text(
                        result.mapNote.isEmpty
                            ? '未配置地图服务，仅显示坐标'
                            : result.mapNote,
                        style: TextStyle(color: theme.colorScheme.error, fontSize: 12),
                      ),
                    ],
                  ],
                ),
              ),
            ),
            if (result?.isDemoData == true) ...[
              const SizedBox(height: 8),
              Card(
                color: theme.colorScheme.tertiaryContainer,
                child: const ListTile(
                  leading: Icon(Icons.science_outlined),
                  title: Text('示例数据'),
                  subtitle: Text('当前地点库是内置示例，不是真实 POI；'
                      '接入 places.json 后会替换。'),
                ),
              ),
            ],
            const SizedBox(height: 12),
            Text('按喜好推荐', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 4,
              children: [
                for (final pref in _prefOptions)
                  FilterChip(
                    label: Text(pref),
                    selected: _prefs.contains(pref),
                    onSelected: _busy
                        ? null
                        : (selected) {
                            setState(() {
                              if (selected) {
                                _prefs = [..._prefs, pref];
                              } else {
                                _prefs = _prefs.where((p) => p != pref).toList();
                              }
                            });
                            _refresh(withLocation: _position != null);
                          },
                  ),
              ],
            ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
            ],
            const SizedBox(height: 12),
            if (result != null)
              ...result.items.map((item) => Card(
                    child: ListTile(
                      title: Text(item.name),
                      subtitle: Text(
                        [
                          item.reason,
                          if (item.description.isNotEmpty) item.description,
                        ].join('\n'),
                      ),
                      isThreeLine: item.description.isNotEmpty,
                      trailing: item.hasCoordinate
                          ? IconButton(
                              icon: const Icon(Icons.navigation_outlined),
                              tooltip: '导航',
                              onPressed: () => _openNavigation(item),
                            )
                          : null,
                    ),
                  )),
            if (result != null && result.items.isEmpty)
              const Padding(
                padding: EdgeInsets.all(24),
                child: Center(child: Text('没有可推荐的地点')),
              ),
          ],
        ),
      ),
    );
  }
}
