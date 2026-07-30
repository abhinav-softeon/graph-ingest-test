/*
 * Ground-truth call bindings for a Java source tree, using javac itself.
 *
 * WHY THIS EXISTS
 * The graph's Java CALLS edges come from heuristic name matching (tree-sitter
 * gives syntax, not types), so their accuracy has never actually been measured
 * — only inferred from edge counts. This runs the real compiler's resolver over
 * the same source and emits what javac says each call site binds to. Diffing
 * the two gives true precision/recall instead of plausibility arguments.
 *
 * WHY IT WORKS WITHOUT A BUILD SYSTEM
 * javac's attribution phase resolves every symbol it CAN. With no classpath the
 * external types (JDK, third-party jars) fail to resolve and are reported as
 * errors — but in-repo class/method binding still succeeds, because those
 * sources are all present via -sourcepath. In-repo calls are the only thing the
 * graph contains, so partial attribution is exactly the ground truth we need.
 * Diagnostics are swallowed deliberately: on a dependency-less tree there will
 * be thousands, and they are expected, not failures.
 *
 * OUTPUT (TSV, one row per resolved in-repo invocation):
 *     callerClass <TAB> callerMethod <TAB> callerArity <TAB> calleeClass
 *                 <TAB> calleeMethod <TAB> calleeArity <TAB> file <TAB> line
 * Both arities are emitted because a graph Function node is keyed by
 * (fqn, param_count) — the extractor gives every overload the same fqn, so the
 * arity is what distinguishes them.
 *
 * Constructors are reported by javac as `<init>`; the graph names them after
 * their class. Consumers normalize (see graph_core/javac_resolver.py) rather
 * than having this tool lie about what the compiler said.
 *
 * Plus a STATS: line to stderr with the resolution breakdown.
 *
 * USAGE
 *     javac -d out scripts/oracle/CallOracle.java
 *     java -cp out CallOracle <source-root> [batchSize] > calls.tsv
 *
 * Java 11+ (uses only the exported com.sun.source API, no --add-exports).
 */

import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.ClassTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;

import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.ExecutableElement;
import javax.lang.model.element.TypeElement;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

import java.io.File;
import java.io.IOException;
import java.io.PrintStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.Stream;

public class CallOracle {

    // Counters — printed to stderr so stdout stays a clean TSV stream.
    static long invocationsSeen = 0;
    static long resolvedInRepo = 0;
    static long resolvedExternal = 0;
    static long unresolved = 0;
    static long filesAttributed = 0;
    static long batchesFailed = 0;

    static java.util.Set<String> inRepoTypes = new java.util.HashSet<>();

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("usage: java CallOracle <source-root> [batchSize]");
            System.exit(2);
        }
        Path root = Paths.get(args[0]).toAbsolutePath();
        // Batching keeps javac's memory bounded. Attribution holds every symbol
        // table entry for the batch, and a 16k-file tree in one task will
        // exhaust a default heap long before it finishes.
        int batchSize = args.length > 1 ? Integer.parseInt(args[1]) : 400;

        List<File> sources;
        try (Stream<Path> walk = Files.walk(root)) {
            sources = walk
                    .filter(p -> p.toString().endsWith(".java"))
                    .filter(p -> {
                        String s = p.toString().replace('\\', '/');
                        // Skip build output and vendored trees — same exclusions
                        // the graph's discovery uses, so both sides see one corpus.
                        return !s.contains("/target/") && !s.contains("/build/")
                                && !s.contains("/out/") && !s.contains("/node_modules/");
                    })
                    .map(Path::toFile)
                    .collect(Collectors.toList());
        }
        System.err.println("[oracle] found " + sources.size() + " java file(s) under " + root);

        // Pre-pass: every top-level type declared in the tree. Used to classify a
        // resolved callee as in-repo vs external WITHOUT needing the callee's own
        // file to have attributed cleanly.
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            System.err.println("[oracle] no system java compiler — run with a JDK, not a JRE");
            System.exit(3);
        }

        PrintStream out = new PrintStream(new java.io.BufferedOutputStream(System.out, 1 << 20), false);

        List<String> sourceRoots = deriveSourceRoots(root, sources);
        String sourcePath = String.join(File.pathSeparator, sourceRoots);
        System.err.println("[oracle] derived " + sourceRoots.size() + " source root(s):");
        for (int i = 0; i < Math.min(sourceRoots.size(), 12); i++) {
            System.err.println("           " + sourceRoots.get(i));
        }
        if (sourceRoots.size() > 12) {
            System.err.println("           ... and " + (sourceRoots.size() - 12) + " more");
        }

        for (int i = 0; i < sources.size(); i += batchSize) {
            List<File> batch = sources.subList(i, Math.min(i + batchSize, sources.size()));
            try {
                processBatch(compiler, root, sourcePath, batch, out);
            } catch (Throwable t) {
                // A batch that javac cannot attribute at all (cyclic missing
                // symbols, malformed source) must not abort the whole run —
                // record it and keep going, so the stats stay honest about
                // coverage rather than silently reporting on a partial corpus.
                batchesFailed++;
                System.err.println("[oracle] batch at " + i + " failed: " + t);
            }
            if ((i / batchSize) % 10 == 0) {
                System.err.println("[oracle] " + Math.min(i + batchSize, sources.size())
                        + "/" + sources.size() + " files, " + resolvedInRepo + " in-repo calls");
            }
        }
        out.flush();

        System.err.println("=== STATS ===");
        System.err.println("files_attributed   " + filesAttributed);
        System.err.println("batches_failed     " + batchesFailed);
        System.err.println("invocations_seen   " + invocationsSeen);
        System.err.println("resolved_in_repo   " + resolvedInRepo);
        System.err.println("resolved_external  " + resolvedExternal);
        System.err.println("unresolved         " + unresolved);
    }

    static void processBatch(JavaCompiler compiler, Path root, String sourcePath,
                              List<File> batch, PrintStream out) throws IOException {
        StandardJavaFileManager fm = compiler.getStandardFileManager(null, null, null);
        Iterable<? extends JavaFileObject> units = fm.getJavaFileObjects(
                batch.toArray(new File[0]));

        List<String> options = Arrays.asList(
                // -proc:none: annotation processors would need their jars and
                // add nothing to call binding.
                "-proc:none",
                // Attribution only — never write class files.
                "-d", System.getProperty("java.io.tmpdir") + File.separator + "calloracle-out",
                // The DERIVED source roots (see deriveSourceRoots) — not the repo
                // root. This is what lets javac resolve in-repo types whose files
                // are not in this batch; with the wrong root nothing cross-batch
                // resolves and coverage collapses to whatever batching grouped.
                "-sourcepath", sourcePath,
                "-nowarn",
                // Missing external deps produce a flood of "cannot find symbol";
                // this keeps javac from bailing after the default 100 errors.
                "-Xmaxerrs", "100000"
        );

        // null diagnostic listener -> diagnostics are discarded. Intentional:
        // on a dependency-less tree they are expected noise, not signal.
        JavacTask task = (JavacTask) compiler.getTask(
                new java.io.PrintWriter(java.io.Writer.nullWriter()),
                fm, d -> { }, options, null, units);

        Trees trees = Trees.instance(task);
        SourcePositions positions = trees.getSourcePositions();

        Iterable<? extends CompilationUnitTree> asts = task.parse();
        // Record declared types before analyze() so classification works even if
        // attribution partially fails.
        for (CompilationUnitTree cu : asts) {
            for (com.sun.source.tree.Tree td : cu.getTypeDecls()) {
                if (td instanceof ClassTree) {
                    String pkg = cu.getPackageName() == null ? "" : cu.getPackageName().toString();
                    String nm = ((ClassTree) td).getSimpleName().toString();
                    inRepoTypes.add(pkg.isEmpty() ? nm : pkg + "." + nm);
                }
            }
        }

        try {
            task.analyze();   // resolve what it can; missing externals stay unresolved
        } catch (Throwable t) {
            // Partial attribution is still useful — scan whatever bound.
            System.err.println("[oracle] analyze() partial: " + t);
        }

        for (CompilationUnitTree cu : asts) {
            filesAttributed++;
            // REPO-RELATIVE, matching what the graph stores in evidence_file
            // (discovery.FileInfo.relpath). An absolute path here would be a
            // path into the temp extraction dir — meaningless once the run ends,
            // and inconsistent with every heuristic edge in the same graph.
            final String file = relRoot(root, relativize(cu.getSourceFile().toUri().getPath()));
            // Emit EVERY attributed file, not just those containing calls. A
            // consumer needs to know which files javac actually resolved so it
            // can defer to the heuristic on the rest — a file with zero calls is
            // still "covered", and inferring coverage from call rows alone would
            // wrongly mark it uncovered. Prefixed so it is trivially separable
            // from call rows in one pass.
            out.print("@FILE\t");
            out.println(file);
            new TreePathScanner<Void, Void>() {
                String enclosingClass = "";
                String enclosingMethod = "";
                int enclosingArity = 0;

                @Override
                public Void visitClass(ClassTree node, Void p) {
                    String prev = enclosingClass;
                    Element el = trees.getElement(getCurrentPath());
                    if (el instanceof TypeElement) {
                        enclosingClass = ((TypeElement) el).getQualifiedName().toString();
                    }
                    super.visitClass(node, p);
                    enclosingClass = prev;
                    return null;
                }

                @Override
                public Void visitMethod(MethodTree node, Void p) {
                    String prev = enclosingMethod;
                    int prevArity = enclosingArity;
                    enclosingMethod = node.getName().toString();
                    // Caller arity is emitted too: a graph Function node is keyed
                    // by (fqn, param_count) because the extractor gives every
                    // overload the same fqn. Without the caller's arity an
                    // overloaded caller cannot be mapped to a single node.
                    enclosingArity = node.getParameters().size();
                    super.visitMethod(node, p);
                    enclosingMethod = prev;
                    enclosingArity = prevArity;
                    return null;
                }

                @Override
                public Void visitMethodInvocation(MethodInvocationTree node, Void p) {
                    invocationsSeen++;
                    TreePath path = getCurrentPath();
                    Element callee = null;
                    try {
                        callee = trees.getElement(
                                new TreePath(path, node.getMethodSelect()));
                    } catch (Throwable ignored) {
                        // getElement can throw on error-typed trees.
                    }
                    if (callee instanceof ExecutableElement
                            && (callee.getKind() == ElementKind.METHOD
                                || callee.getKind() == ElementKind.CONSTRUCTOR)) {
                        ExecutableElement m = (ExecutableElement) callee;
                        Element owner = m.getEnclosingElement();
                        if (owner instanceof TypeElement) {
                            String ownerFqn = ((TypeElement) owner).getQualifiedName().toString();
                            if (inRepoTypes.contains(ownerFqn)) {
                                resolvedInRepo++;
                                long pos = positions.getStartPosition(cu, node);
                                long line = pos >= 0 ? cu.getLineMap().getLineNumber(pos) : 0;
                                out.print(enclosingClass);   out.print('\t');
                                out.print(enclosingMethod);  out.print('\t');
                                out.print(enclosingArity);   out.print('\t');
                                out.print(ownerFqn);         out.print('\t');
                                out.print(m.getSimpleName());out.print('\t');
                                out.print(m.getParameters().size()); out.print('\t');
                                out.print(file);             out.print('\t');
                                out.println(line);
                            } else {
                                resolvedExternal++;
                            }
                        } else {
                            unresolved++;
                        }
                    } else {
                        unresolved++;
                    }
                    return super.visitMethodInvocation(node, p);
                }
            }.scan(cu, null);
        }
        fm.close();
    }

    /** Path relative to the source root, forward-slashed — the form the graph
     *  stores in evidence_file, so consumers can match without re-deriving it. */
    static String relRoot(Path root, String absolute) {
        String r = root.toString().replace('\\', '/');
        if (!r.endsWith("/")) r = r + "/";
        String a = absolute.replace('\\', '/');
        if (a.regionMatches(true, 0, r, 0, r.length())) {
            return a.substring(r.length());
        }
        return a;
    }

    /**
     * Source roots to hand javac on -sourcepath, derived from each file's own
     * package declaration.
     *
     * This is not a nicety — it decides how much javac can resolve at all. javac
     * finds a type `com.foo.Bar` by looking for `<sourcepath>/com/foo/Bar.java`.
     * Passing the REPO ROOT fails for the standard Maven layout, where the file
     * actually lives at `src/main/java/com/foo/Bar.java`: nothing is found, and
     * only types that happen to fall in the same compilation batch resolve. That
     * silently caps coverage at "whatever the batching accidentally grouped
     * together" — which looks like partial attribution but is really a
     * misconfiguration.
     *
     * So: for each file, read its `package a.b.c;` and strip `a/b/c/File.java`
     * off the end of its path. What remains is a real source root. Collecting
     * the distinct set handles multi-module repos (many nested src/main/java
     * directories) with no assumptions about layout. Only the header is read,
     * up to the package line, so this is cheap even over tens of thousands
     * of files.
     */
    static java.util.List<String> deriveSourceRoots(Path root, List<File> sources) {
        java.util.Set<String> roots = new java.util.LinkedHashSet<>();
        java.util.regex.Pattern pkgPat = java.util.regex.Pattern.compile(
                "^\\s*package\\s+([\\w.]+)\\s*;");
        for (File f : sources) {
            String pkg = null;
            try (java.io.BufferedReader br = Files.newBufferedReader(
                    f.toPath(), java.nio.charset.StandardCharsets.UTF_8)) {
                String line;
                int scanned = 0;
                while ((line = br.readLine()) != null && scanned++ < 200) {
                    java.util.regex.Matcher m = pkgPat.matcher(line);
                    if (m.find()) { pkg = m.group(1); break; }
                    // A type declaration means there was no package: default
                    // package, so the file's own directory is the root.
                    String t = line.trim();
                    if (t.startsWith("class ") || t.startsWith("public ")
                            || t.startsWith("interface ") || t.startsWith("enum ")) {
                        break;
                    }
                }
            } catch (Exception ignored) {
                continue;
            }
            String dir = f.getAbsoluteFile().getParent();
            if (dir == null) continue;
            dir = dir.replace('\\', '/');
            if (pkg == null || pkg.isEmpty()) {
                roots.add(dir);
                continue;
            }
            String suffix = "/" + pkg.replace('.', '/');
            if (dir.endsWith(suffix)) {
                roots.add(dir.substring(0, dir.length() - suffix.length()));
            } else {
                // Path does not mirror the package (generated sources, odd
                // layout). Nothing reliable to strip — skip rather than add a
                // bogus root that would slow every lookup down.
                continue;
            }
        }
        if (roots.isEmpty()) roots.add(root.toString().replace('\\', '/'));
        return new ArrayList<>(roots);
    }

    static String relativize(String uriPath) {
        if (uriPath == null) return "";
        String s = uriPath.replace('\\', '/');
        if (s.length() > 2 && s.charAt(0) == '/' && s.charAt(2) == ':') s = s.substring(1);
        return s;
    }
}
