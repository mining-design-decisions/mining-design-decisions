pub enum Marker {
    Attachment,
    ClassName,
    CloudInstanceSpec,
    Date,
    FilePath,
    FormattedLogging,
    FormattedTraceback,
    GithubLink,
    ImageAttachment,
    InlineCode,
    IssueLink,
    IPAddress,
    Log,
    MethodOrVariableName,
    NoFormatBlock,
    PackageName,
    SimpleClassName,
    SimpleMethodOrVariableName,
    StorageSize,
    StructuredCodeBlock,
    TechnologyName,
    Traceback,
    UnformattedLog,
    UnformattedTraceback,
    UserProfileLink,
    VersionNumber,
    WebLink
}

impl Marker {
    pub fn all_markers() -> Vec<Marker> {
        vec![]
    }

    pub fn string_marker(&self) -> String {
        match self {
            Self::Attachment => "ATTACHMENT",
            Self::ClassName => "CLASSNAME",
            Self::CloudInstanceSpec => "CLOUDINSTANCE",
            Self::Date => "DATE",
            Self::FilePath => "FILEPATH",
            Self::FormattedLogging => "FORMATTEDLOGGINGOUTPUT",
            Self::FormattedTraceback => "FORMATTEDTRACEBACK",
            Self::GithubLink => "GITHUBLINK",
            Self::ImageAttachment => "IMAGEATTACHMENT",
            Self::InlineCode => "INLINECODESAMPLE",
            Self::IssueLink => "ISSUELINK",
            Self::IPAddress => "IP ADDRESS",
            Self::Log => "LLLOG",
            Self::MethodOrVariableName => "METHODORVARIABLENAME",
            Self::NoFormatBlock => "NOFORMATBLOCK",
            Self::PackageName => "PACKAGE",
            Self::SimpleClassName => "SIMPLECLASSNAME",
            Self::SimpleMethodOrVariableName => "SIMPLEMETHODORVARIABLENAME",
            Self::StorageSize => "STORAGESIZE",
            Self::StructuredCodeBlock => "STRUCTUREDCODEBLOCK",
            Self::TechnologyName => "TECHNOLOGYNAMES",
            Self::Traceback => "TTTRACEBACK",
            Self::UnformattedLog => "UNFORMATTEDLOGGINGOUTPUT",
            Self::UnformattedTraceback => "UNFORMATTEDTRACEBACK",
            Self::UserProfileLink => "USERPROFILELINK",
            Self::VersionNumber => "VERSIONNUMBER",
            Self::WebLink => "WEBLINK"
        }.into()
    }
}