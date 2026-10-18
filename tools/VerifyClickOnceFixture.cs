// Offline fixture check. Reads XML and PE metadata; never activates an application.
using System;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Xml;

internal static class VerifyClickOnceFixture
{
    const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;
    static XmlDocument Read(string path) { XmlDocument value = new XmlDocument(); value.XmlResolver = null; value.Load(path); return value; }
    static XmlElement One(XmlNode node, string xpath) { return (XmlElement)node.SelectSingleNode(xpath); }
    static void Require(bool condition, string message) { if (!condition) throw new InvalidDataException(message); }
    static string Identity(XmlElement node)
    {
        return node.GetAttribute("name") + ", Version=" + node.GetAttribute("version") + ", Culture=" +
            (node.GetAttribute("language") == "" ? "neutral" : node.GetAttribute("language")) + ", PublicKeyToken=" +
            (node.GetAttribute("publicKeyToken") == "" ? "null" : node.GetAttribute("publicKeyToken")) +
            ", processorArchitecture=" + node.GetAttribute("processorArchitecture");
    }
    static string Token(AssemblyName value) { return BitConverter.ToString(value.GetPublicKeyToken() ?? new byte[0]).Replace("-", "").ToLowerInvariant(); }
    static void Same(AssemblyName expected, AssemblyName actual, string context)
    {
        Require(expected.Name == actual.Name && expected.Version == actual.Version &&
            (expected.CultureName ?? "") == (actual.CultureName ?? "") && Token(expected) == Token(actual) &&
            expected.ProcessorArchitecture == actual.ProcessorArchitecture,
            context + " identity mismatch: expected " + expected.FullName + "; actual " + actual.FullName);
    }
    static void Hash(XmlElement reference, string path)
    {
        XmlElement algorithm = One(reference, "./*[local-name()='hash']/*[local-name()='DigestMethod']");
        XmlElement digest = One(reference, "./*[local-name()='hash']/*[local-name()='DigestValue']");
        Require(algorithm != null && digest != null && algorithm.GetAttribute("Algorithm").EndsWith("#sha256"), "Expected SHA256 file hash");
        using (SHA256 sha = SHA256.Create())
            Require(Convert.ToBase64String(sha.ComputeHash(File.ReadAllBytes(path))) == digest.InnerText.Trim(), "Hash mismatch: " + path);
    }
    static int Main(string[] args)
    {
        try
        {
            string deploymentPath = Path.GetFullPath(args[0]);
            XmlDocument deployment = Read(deploymentPath);
            XmlElement applicationReference = One(deployment, "/*[local-name()='assembly']/*[local-name()='dependency']/*[local-name()='dependentAssembly']");
            string applicationPath = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(deploymentPath), applicationReference.GetAttribute("codebase")));
            Hash(applicationReference, applicationPath);
            XmlDocument application = Read(applicationPath);
            XmlElement applicationIdentity = One(application, "/*[local-name()='assembly']/*[local-name()='assemblyIdentity']");
            Same(new AssemblyName(Identity(One(applicationReference, "./*[local-name()='assemblyIdentity']"))), new AssemblyName(Identity(applicationIdentity)), "Deployment application");
            XmlElement entry = One(application, "/*[local-name()='assembly']/*[local-name()='entryPoint']");
            string executableName = One(entry, "./*[local-name()='commandLine']").GetAttribute("file");
            string executable = Path.Combine(Path.GetDirectoryName(applicationPath), executableName);
            XmlElement executableReference = null;
            foreach (XmlElement reference in application.SelectNodes("/*[local-name()='assembly']/*[local-name()='dependency']/*[local-name()='dependentAssembly'][@dependencyType='install']"))
            {
                string file = Path.Combine(Path.GetDirectoryName(applicationPath), reference.GetAttribute("codebase"));
                Hash(reference, file);
                if (reference.GetAttribute("codebase") == executableName) executableReference = reference;
            }
            Require(executableReference != null, "Entrypoint dependency missing");
            AssemblyName expected = new AssemblyName(Identity(One(executableReference, "./*[local-name()='assemblyIdentity']")));
            Same(expected, new AssemblyName(Identity(One(entry, "./*[local-name()='assemblyIdentity']"))), "Entrypoint");
            Same(expected, AssemblyName.GetAssemblyName(executable), "Managed EXE");
            // This is the identity ClickOnce actually prefers (PE manifest first,
            // then managed metadata). Signature-only/Mage validation misses this.
            Assembly framework = Assembly.Load("System.Deployment, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a");
            Type manifestType = framework.GetType("System.Deployment.Application.Manifest.AssemblyManifest", true);
            object manifest = manifestType.GetConstructor(Flags, null, new Type[] { typeof(string) }, null).Invoke(new object[] { executable });
            object actualIdentity = manifestType.GetProperty("Identity", Flags).GetValue(manifest, null);
            Same(expected, new AssemblyName(actualIdentity.ToString()), "ClickOnce extracted EXE");
            Require(Path.GetFileName(applicationPath) == applicationIdentity.GetAttribute("name") + ".manifest", "Application manifest filename differs from identity");
            foreach (XmlElement file in application.SelectNodes("/*[local-name()='assembly']/*[local-name()='file']"))
                Hash(file, Path.Combine(Path.GetDirectoryName(applicationPath), file.GetAttribute("name")));
            Console.WriteLine("PASS: deployment/application/entrypoint/managed EXE/ClickOnce extracted identity and all payload SHA256 hashes agree.");
            Console.WriteLine("ClickOnce extracted EXE: " + actualIdentity);
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error.Message); return 1; }
    }
}
